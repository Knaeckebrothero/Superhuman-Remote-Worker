"""Slice 3 compute activation and agent binding foundation contracts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest

from orchestrator.database.migrate import run_migrations
from orchestrator.services.infrastructure_metering.collectors.vmi_normalization import (
    normalize_virtual_machine_instance,
)
from orchestrator.services.infrastructure_metering.compute_activation import (
    ComputeActivationStore,
    ComputeActivationConflict,
    ComputeActivationContractError,
    ComputeActivationNotReady,
    advance_compute_activation_to_shadow,
    confirm_compute_authority_snapshot,
    lock_compute_activation,
    promote_compute_recovery_epochs,
    promote_compute_scope_epochs,
    read_compute_activations,
    schedule_compute_activation,
)
from orchestrator.services.infrastructure_metering.inventory import (
    InventoryItem,
    InventoryScopeIdentity,
    InventoryStore,
    SnapshotFinalization,
    SnapshotObservationContext,
    TransportNonceClaim,
    inventory_manifest_digest,
)
from orchestrator.services.infrastructure_metering.vmi_intervals import (
    VMIIntervalReconciler,
)
from src.shared.workspace_contract import (
    WORKSPACE_DISPATCH_AUTHORITY_CONTEXT_KEY,
    pinned_dispatch_authority_jsonb_sql,
)


ROOT = Path(__file__).parents[1]
APP_MIGRATIONS = ROOT / "orchestrator/database/migrations/app"
COMPUTE_MIGRATION = APP_MIGRATIONS / "0103_compute_metering_foundations.sql"
COMPUTE_SCOPE_GUARD_MIGRATION = APP_MIGRATIONS / "0106_compute_scope_epoch_guard.sql"
COMPUTE_ACTIVATION_SERVICE = (
    ROOT / "orchestrator/services/infrastructure_metering/compute_activation.py"
)


def _asyncpg_dsn(url: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def _swap_db(dsn: str, dbname: str) -> str:
    head, _, tail = dsn.rpartition("/")
    query = ""
    if "?" in tail:
        query = "?" + tail.split("?", 1)[1]
    return f"{head}/{dbname}{query}"


def _transport(collector_id: str, request_kind: str) -> TransportNonceClaim:
    return TransportNonceClaim(
        collector_id=collector_id,
        request_nonce=uuid4(),
        request_kind=request_kind,
        request_digest="9" * 64,
    )


@pytest.fixture(scope="module")
def compute_pg_dsn() -> str:
    testcontainers = pytest.importorskip("testcontainers.postgres")
    try:
        container = testcontainers.PostgresContainer("postgres:16")
        container.start()
    except Exception as exc:
        pytest.skip(f"no container runtime for compute migration test: {exc}")
    try:
        yield _asyncpg_dsn(container.get_connection_url())
    finally:
        container.stop()


def test_compute_migration_is_independent_dark_launch_and_raw_free() -> None:
    sql = COMPUTE_MIGRATION.read_text()
    compact = " ".join(sql.split())

    for table in (
        "compute_metering_activation",
        "compute_shadow_observations",
        "agent_metering_pod_identity_state",
        "agent_metering_binding_events",
    ):
        assert f"CREATE TABLE {table}" in sql

    assert "VALUES ('agent_pod'), ('ide_workspace_pod'), ('workspace_vm')" in compact
    assert "disabled -> shadow -> future active" in sql
    assert "resource_intervals_compute_activation_guard" in sql
    assert "NEW.details->>'product_class' = 'ide-session'" in sql
    assert "NEW.started_at < activation_time" in sql
    assert "compute_shadow_observations_immutable" in sql
    assert "agent_metering_binding_events_append_only" in sql

    shadow_table = sql.split("CREATE TABLE compute_shadow_observations", 1)[1].split(
        ");", 1
    )[0]
    assert "resource_interval_id" not in shadow_table
    assert "publication_plan" not in shadow_table

    assert "job_row.status = 'processing'" in sql
    assert "job_row.assigned_agent_id = target_agent_id" in sql
    assert "thread_row.status IN ('active', 'awaiting_user')" in sql
    assert "thread_row.agent_id = target_agent_id" in sql
    assert "duplicate-pod-uid" in sql
    assert "missing-pod-uid" in sql


def test_compute_scope_guard_requires_the_exact_current_promoted_epoch() -> None:
    sql = COMPUTE_SCOPE_GUARD_MIGRATION.read_text()
    compact = " ".join(sql.split())

    assert "depends-on:    0105_storage_source_activation.sql" in sql
    assert "CREATE FUNCTION enforce_resource_interval_compute_scope_epoch()" in sql
    assert "resource_intervals_compute_scope_epoch_guard" in sql
    assert "epoch.retired_at IS NULL" in sql
    assert "epoch.required_for_rollup" in sql
    assert "NEW.started_at < epoch_boundary" in sql
    assert "statement_timestamp() < epoch_boundary" in sql
    assert "scope.collector_id = expected_collector" in sql
    assert "scope.api_resource = expected_resource" in sql
    assert "Scope expansion after activation is intentionally unsupported" in sql
    assert (
        "BEFORE INSERT ON resource_intervals FOR EACH ROW EXECUTE FUNCTION "
        "enforce_resource_interval_compute_scope_epoch()" in compact
    )


def test_all_compute_epoch_update_locks_use_canonical_uuid_order() -> None:
    source = COMPUTE_ACTIVATION_SERVICE.read_text()

    assert source.count('"ORDER BY epoch.id FOR UPDATE OF epoch"') == 3
    assert '"ORDER BY scope.namespace FOR UPDATE OF epoch"' not in source


@pytest.mark.asyncio
async def test_compute_activation_clamps_and_rejects_an_uncrossed_boundary() -> None:
    boundary = datetime(2026, 8, 7, tzinfo=timezone.utc)
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "activation_key": "agent_pod",
        "state": "active",
        "activated_at": boundary,
        "database_time": boundary + timedelta(minutes=5),
    }

    assert (
        await lock_compute_activation(
            conn,
            activation_key="agent_pod",
            observed_started_at=boundary - timedelta(hours=3),
        )
        == boundary
    )
    observed = boundary + timedelta(minutes=2)
    assert (
        await lock_compute_activation(
            conn,
            activation_key="agent_pod",
            observed_started_at=observed,
        )
        == observed
    )

    conn.fetchrow.return_value = {
        "activation_key": "agent_pod",
        "state": "active",
        "activated_at": boundary,
        "database_time": boundary - timedelta(microseconds=1),
    }
    with pytest.raises(ComputeActivationNotReady, match="future"):
        await lock_compute_activation(
            conn,
            activation_key="agent_pod",
            observed_started_at=boundary,
        )


@pytest.mark.asyncio
async def test_compute_activation_requires_future_midnight_and_replays_exactly() -> (
    None
):
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    boundary = datetime(2026, 8, 7, tzinfo=timezone.utc)
    conn = AsyncMock()

    with pytest.raises(ComputeActivationContractError, match="UTC midnight"):
        await schedule_compute_activation(
            conn,
            activation_key="workspace_vm",
            activated_at=now,
        )
    conn.fetchrow.assert_not_awaited()

    conn.fetchrow.return_value = {
        "activation_key": "workspace_vm",
        "state": "shadow",
        "activated_at": None,
        "database_time": boundary,
    }
    with pytest.raises(ComputeActivationContractError, match="future UTC midnight"):
        await schedule_compute_activation(
            conn,
            activation_key="workspace_vm",
            activated_at=boundary,
        )

    conn.fetchrow.return_value = {
        "activation_key": "workspace_vm",
        "state": "active",
        "activated_at": boundary,
        "database_time": boundary + timedelta(hours=1),
    }
    replay = await schedule_compute_activation(
        conn,
        activation_key="workspace_vm",
        activated_at=boundary,
    )
    assert replay.state == "active"
    assert replay.activated_at == boundary


def _healthy_scope_row(
    *,
    now: datetime,
    boundary: datetime,
    required_for_rollup: bool = False,
) -> dict[str, object]:
    scope_id = uuid4()
    epoch_id = uuid4()
    return {
        "inventory_scope_id": scope_id,
        "inventory_scope_epoch_id": epoch_id,
        "proof_snapshot_id": uuid4(),
        "namespace": "workers",
        "required_for_rollup": required_for_rollup,
        "required_from": boundary - timedelta(days=1) if required_for_rollup else None,
        "reliable_from": boundary - timedelta(hours=2),
        "continuous_since": boundary - timedelta(hours=2),
        "last_complete_at": now - timedelta(minutes=1),
        "snapshot_health": "healthy",
        "item_health": "healthy",
        "continuity_health": "healthy",
        "backend_health": "healthy",
        "leader_generation": 7,
        "snapshot_leader_generation": 7,
        "item_count": 2,
        "complete": True,
        "manifest_state": "sealed",
        "received_at": now - timedelta(minutes=1),
        "shadow_count": 2,
        "missing_shadow_count": 0,
        "orphan_shadow_count": 0,
    }


def _workspace_vm_item() -> InventoryItem:
    normalized = normalize_virtual_machine_instance(
        {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachineInstance",
            "metadata": {
                "uid": "recovery-vmi-uid",
                "namespace": "agent-vms",
                "name": "recovery-vm",
                "resourceVersion": "rv-vmi-recovery",
                "creationTimestamp": "2026-08-25T08:00:00Z",
                "ownerReferences": [
                    {
                        "apiVersion": "kubevirt.io/v1",
                        "kind": "VirtualMachine",
                        "name": "recovery-vm",
                        "uid": "recovery-vm-uid",
                    }
                ],
            },
            "spec": {
                "domain": {
                    "cpu": {"cores": 2, "sockets": 1, "threads": 1},
                    "memory": {"guest": "8Gi"},
                }
            },
            "status": {
                "phase": "Running",
                "nodeName": "worker-a",
                "phaseTransitionTimestamps": [
                    {
                        "phase": "Scheduled",
                        "phaseTransitionTimestamp": "2026-08-25T08:00:02Z",
                    }
                ],
            },
        }
    )
    return InventoryItem(
        source_kind="vmi",
        source_uid=normalized.uid,
        revision_hash=normalized.revision_hash,
        normalized_item=normalized.to_db_item(),
        valid_for_metering=normalized.valid_for_metering,
    )


def _scope_proof_connection(
    *,
    now: datetime,
    rows: list[dict[str, object]],
    generation: int = 7,
    activation_key: str = "agent_pod",
) -> AsyncMock:
    conn = AsyncMock()

    async def fetchrow(query, *args):  # noqa: ANN001
        if "FROM compute_metering_epoch_promotion_requests" in query:
            return None
        if "compute-activation:lock-update" in query:
            return {
                "activation_key": activation_key,
                "state": "shadow",
                "activated_at": None,
                "database_time": now,
            }
        if query.startswith("INSERT INTO compute_metering_epoch_promotion_requests"):
            return {"promoted_at": now}
        raise AssertionError(query)

    async def fetch(query, *args):  # noqa: ANN001
        if query.startswith("SELECT id,namespace FROM resource_inventory_scopes"):
            return [
                {
                    "id": row["inventory_scope_id"],
                    "namespace": row["namespace"],
                }
                for row in rows
            ]
        if "missing_shadow_count" in query:
            return rows
        if query.startswith("SELECT inventory_scope_id,inventory_scope_epoch_id"):
            return []
        if "FROM compute_metering_epoch_authorities AS authority" in query:
            request_id = args[1]
            return [
                {
                    "id": uuid4(),
                    "activation_key": activation_key,
                    "collector_id": "kubernetes-pods",
                    "source_cluster": "cluster-a",
                    "inventory_scope_id": row["inventory_scope_id"],
                    "inventory_scope_epoch_id": row["inventory_scope_epoch_id"],
                    "previous_authority_id": None,
                    "predecessor_epoch_id": None,
                    "authority_sequence": 1,
                    "effective_from": datetime(2026, 8, 7, tzinfo=timezone.utc),
                    "effective_to": None,
                    "proof_snapshot_id": row["proof_snapshot_id"],
                    "proof_generation": generation,
                    "promotion_request_id": request_id,
                    "namespace": row["namespace"],
                    "is_current_epoch": True,
                }
                for row in rows
            ]
        raise AssertionError(query)

    conn.fetchrow.side_effect = fetchrow
    conn.fetch.side_effect = fetch

    async def fetchval(query, *args):  # noqa: ANN001
        if "leader_generation FROM infra_metering_control" in query:
            return generation
        if query == "SELECT statement_timestamp()":
            return now
        if query.startswith("UPDATE resource_inventory_scope_epochs"):
            assert args == (
                rows[0]["inventory_scope_epoch_id"],
                datetime(2026, 8, 7, tzinfo=timezone.utc),
            )
            return True
        raise AssertionError(query)

    conn.fetchval.side_effect = fetchval
    return conn


@pytest.mark.asyncio
async def test_compute_scope_promotion_requires_exact_fresh_shadow_proof() -> None:
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    boundary = datetime(2026, 8, 7, tzinfo=timezone.utc)
    healthy = _healthy_scope_row(now=now, boundary=boundary)
    conn = _scope_proof_connection(now=now, rows=[healthy])
    request_id = uuid4()

    await promote_compute_scope_epochs(
        conn,
        activation_key="agent_pod",
        activated_at=boundary,
        source_cluster="cluster-a",
        namespaces=("workers",),
        max_scope_age=timedelta(minutes=15),
        expected_generation=7,
        request_id=request_id,
        actor_id=uuid4(),
        audit_reason="reviewed exact shadow proof",
    )
    query, *args = conn.fetch.await_args_list[1].args
    assert "missing_shadow_count" in query
    assert "orphan_shadow_count" in query
    assert "ORDER BY epoch.id FOR UPDATE OF epoch" in query
    assert tuple(args) == (
        "kubernetes-pods",
        "cluster-a",
        "core/v1/pods",
        "agent_pod",
        [healthy["inventory_scope_id"]],
    )
    requirement_rows = conn.executemany.await_args.args[1]
    assert requirement_rows == [
        (
            "agent_pod",
            "kubernetes-pods",
            "cluster-a",
            healthy["inventory_scope_id"],
            healthy["inventory_scope_epoch_id"],
            boundary,
            healthy["proof_snapshot_id"],
            7,
            request_id,
        )
    ]

    orphan = dict(healthy, orphan_shadow_count=1)
    orphan_conn = _scope_proof_connection(now=now, rows=[orphan])
    with pytest.raises(ComputeActivationConflict, match="item-for-item"):
        await promote_compute_scope_epochs(
            orphan_conn,
            activation_key="agent_pod",
            activated_at=boundary,
            source_cluster="cluster-a",
            namespaces=("workers",),
            max_scope_age=timedelta(minutes=15),
            expected_generation=7,
            request_id=uuid4(),
            actor_id=uuid4(),
            audit_reason="reviewed orphan proof rejection",
        )

    generation_conn = _scope_proof_connection(
        now=now,
        rows=[healthy],
        generation=8,
    )
    with pytest.raises(ComputeActivationConflict, match="another collector generation"):
        await promote_compute_scope_epochs(
            generation_conn,
            activation_key="agent_pod",
            activated_at=boundary,
            source_cluster="cluster-a",
            namespaces=("workers",),
            max_scope_age=timedelta(minutes=15),
            expected_generation=7,
            request_id=uuid4(),
            actor_id=uuid4(),
            audit_reason="reviewed stale generation rejection",
        )
    # Stable scope identity is resolved before the generation fence; neither
    # exact epoch nor activation is locked when the generation is stale.
    assert generation_conn.fetch.await_count == 1


@pytest.mark.asyncio
async def test_shared_pod_scope_keeps_its_earlier_required_boundary() -> None:
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    boundary = datetime(2026, 8, 7, tzinfo=timezone.utc)
    shared = _healthy_scope_row(
        now=now,
        boundary=boundary,
        required_for_rollup=True,
    )
    conn = _scope_proof_connection(
        now=now,
        rows=[shared],
        activation_key="ide_workspace_pod",
    )

    await promote_compute_scope_epochs(
        conn,
        activation_key="ide_workspace_pod",
        activated_at=boundary,
        source_cluster="cluster-a",
        namespaces=("workers",),
        max_scope_age=timedelta(minutes=15),
        expected_generation=7,
        request_id=uuid4(),
        actor_id=uuid4(),
        audit_reason="reviewed shared Pod proof",
    )

    assert conn.fetchval.await_count == 2


@pytest.mark.asyncio
async def test_compute_authority_confirmation_requires_exact_live_list_proof() -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [{"activation_key": "agent_pod"}]
    snapshot_id, epoch_id, scope_id = uuid4(), uuid4(), uuid4()
    received_at = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)

    closed = await confirm_compute_authority_snapshot(
        conn,
        activation_keys=("agent_pod",),
        snapshot_id=snapshot_id,
        scope_epoch_id=epoch_id,
        inventory_scope_id=scope_id,
        received_at=received_at,
    )

    assert closed == ("agent_pod",)
    query, *args = conn.fetch.await_args.args
    assert "snapshot.scope_epoch_id=$3" in query
    assert "snapshot.inventory_scope_id=$4" in query
    assert "snapshot.manifest_state='staging'" in query
    assert "ticket.bound_snapshot_id=snapshot.id" in query
    assert "ticket.leader_generation=control.leader_generation" in query
    assert "observation.disposition IN" in query
    assert "'eligible-unpriced','identity-ambiguous'" in query
    assert "interval.compute_scope_epoch_id=$3" in query
    assert "interval.source_revision=item.revision_hash" in query
    assert "interval.cpu_millicores IS NOT DISTINCT FROM" in query
    assert "interval.attribution_scope=observation.attribution_scope" in query
    assert tuple(args) == (
        ["agent_pod"],
        snapshot_id,
        epoch_id,
        scope_id,
        received_at,
    )


@pytest.mark.asyncio
async def test_workspace_vm_initial_authority_uses_healthy_recovery_epoch(
    compute_pg_dsn: str,
) -> None:
    dbname = f"compute_vm_recovery_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(compute_pg_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await admin.close()

    pool = await asyncpg.create_pool(
        _swap_db(compute_pg_dsn, dbname),
        min_size=1,
        max_size=3,
    )
    assert pool is not None
    try:
        await run_migrations(pool, APP_MIGRATIONS)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE infra_metering_control SET leader_generation=7 "
                "WHERE singleton=TRUE"
            )
            await advance_compute_activation_to_shadow(conn, "workspace_vm")
            database_time = await conn.fetchval("SELECT statement_timestamp()")
            assert isinstance(database_time, datetime)

            scope_id, predecessor_epoch_id, recovery_epoch_id = (
                uuid4(),
                uuid4(),
                uuid4(),
            )
            await conn.execute(
                "INSERT INTO resource_inventory_scopes ("
                "id,collector_id,source_cluster,api_resource,namespace) VALUES ("
                "$1,'kubevirt-vmis','cluster-a',"
                "'kubevirt.io/v1/virtualmachineinstances','agent-vms')",
                scope_id,
            )
            await conn.execute(
                "INSERT INTO resource_inventory_scope_epochs ("
                "id,scope_id,epoch_number,coverage_mode,leader_generation) "
                "VALUES ($1,$2,1,'list-watch',7)",
                predecessor_epoch_id,
                scope_id,
            )
            await conn.execute(
                "UPDATE resource_inventory_scope_epochs SET retired_at=$2 WHERE id=$1",
                predecessor_epoch_id,
                database_time,
            )
            await conn.execute(
                "INSERT INTO resource_inventory_scope_epochs ("
                "id,scope_id,epoch_number,coverage_mode,leader_generation,"
                "recovery_from_epoch_id,require_after_recovery) VALUES ("
                "$1,$2,2,'list-watch',7,$3,TRUE)",
                recovery_epoch_id,
                scope_id,
                predecessor_epoch_id,
            )

        store = InventoryStore(pool)
        await store.activate_generation(expected_generation=7)
        scope = InventoryScopeIdentity(
            collector_id="kubevirt-vmis",
            source_cluster="cluster-a",
            api_resource="kubevirt.io/v1/virtualmachineinstances",
            namespace="agent-vms",
        )
        item = _workspace_vm_item()
        assert item.valid_for_metering
        ticket = await store.issue_ingest_ticket(
            recovery_epoch_id,
            "a" * 64,
            scope=scope,
            transport=_transport(scope.collector_id, "snapshot-ticket"),
            max_snapshot_items=10,
            max_snapshot_bytes=100_000,
        )
        snapshot_id = uuid4()
        await store.begin_snapshot(
            ticket.token,
            ticket.id,
            snapshot_id,
            database_time - timedelta(minutes=2),
            scope=scope,
            transport=_transport(scope.collector_id, "snapshot-begin"),
        )
        await store.stage_items(
            ticket.token,
            ticket.id,
            snapshot_id,
            (item,),
            scope=scope,
            transport=_transport(scope.collector_id, "snapshot-items"),
        )
        final = SnapshotFinalization(
            collection_completed_at=database_time - timedelta(minutes=1),
            complete=True,
            item_count=1,
            item_digest=inventory_manifest_digest((item,)),
            resource_version="rv-vmi-recovery",
        )
        reconciler = VMIIntervalReconciler(shadow_enabled=True)
        finalized = await store.finalize_snapshot(
            ticket.token,
            ticket.id,
            snapshot_id,
            final,
            scope=scope,
            transport=_transport(scope.collector_id, "snapshot-finalize"),
            interval_mutator=reconciler.apply_snapshot,
            observation_hook=reconciler.observe_snapshot,
            require_shadow_comparison=True,
        )
        assert finalized.pending_valid_items == 0
        assert finalized.shadow_comparisons == 1

        replayed = await store.finalize_snapshot(
            ticket.token,
            ticket.id,
            snapshot_id,
            final,
            scope=scope,
            transport=_transport(scope.collector_id, "snapshot-finalize"),
        )
        assert replayed.replayed

        async with pool.acquire() as conn:
            comparison = await conn.fetchrow(
                "SELECT status,reason_code,explained,owner_kind,owner_id,"
                "owner_trusted,observed_cpu_millicores,observed_memory_bytes,"
                "observed_started_at,observed_start_time_source,"
                "observed_start_uncertainty_us,comparison_at "
                "FROM resource_inventory_shadow_comparisons "
                "WHERE snapshot_id=$1 AND source_uid=$2",
                snapshot_id,
                item.source_uid,
            )
            assert comparison is not None
            assert (
                comparison["status"],
                comparison["reason_code"],
                comparison["explained"],
            ) == ("not-applicable", "vmi-no-legacy-interval", True)
            assert (
                comparison["owner_kind"],
                comparison["owner_id"],
                comparison["owner_trusted"],
            ) == (None, None, False)
            assert (
                comparison["observed_cpu_millicores"],
                comparison["observed_memory_bytes"],
            ) == (2000, 8 * 1024**3)
            assert comparison["observed_started_at"] == datetime(
                2026, 8, 25, 8, 0, 2, tzinfo=timezone.utc
            )
            assert (
                comparison["observed_start_time_source"],
                comparison["observed_start_uncertainty_us"],
            ) == ("vmi-scheduled-transition", 0)
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM resource_inventory_shadow_comparisons "
                    "WHERE snapshot_id=$1 AND source_uid=$2",
                    snapshot_id,
                    item.source_uid,
                )
                == 1
            )
            epoch_health = await conn.fetchrow(
                "SELECT item_health,continuity_health,reliable_from,"
                "continuous_since,required_from "
                "FROM resource_inventory_scope_epochs "
                "WHERE id=$1",
                recovery_epoch_id,
            )
            assert epoch_health is not None
            assert (
                epoch_health["item_health"],
                epoch_health["continuity_health"],
            ) == ("healthy", "healthy")
            boundary = epoch_health["required_from"]
            assert isinstance(boundary, datetime)
            assert epoch_health["reliable_from"] <= boundary
            assert epoch_health["continuous_since"] <= boundary

            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="initial compute epoch authority is invalid",
            ):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO resource_inventory_coverage_gaps ("
                        "scope_epoch_id,gap_start,reason) VALUES ("
                        "$1,$2,'test-unresolved-recovery-gap')",
                        recovery_epoch_id,
                        database_time,
                    )
                    await conn.execute(
                        "INSERT INTO compute_metering_scope_requirements ("
                        "activation_key,collector_id,source_cluster,"
                        "inventory_scope_id,inventory_scope_epoch_id,"
                        "required_from) VALUES ("
                        "'workspace_vm','kubevirt-vmis','cluster-a',$1,$2,$3)",
                        scope_id,
                        recovery_epoch_id,
                        boundary,
                    )
                    rejected_request_id = uuid4()
                    await conn.execute(
                        "INSERT INTO compute_metering_epoch_promotion_requests ("
                        "id,activation_key,request_kind,collector_id,"
                        "source_cluster,request_digest,actor_id,audit_reason,"
                        "promoted_at) VALUES ("
                        "$1,'workspace_vm','initial-activation',"
                        "'kubevirt-vmis','cluster-a',$2,$3,$4,"
                        "statement_timestamp())",
                        rejected_request_id,
                        "b" * 64,
                        uuid4(),
                        "reject unresolved recovery gap",
                    )
                    await conn.execute(
                        "INSERT INTO compute_metering_epoch_authorities ("
                        "activation_key,collector_id,source_cluster,"
                        "inventory_scope_id,inventory_scope_epoch_id,"
                        "previous_authority_id,predecessor_epoch_id,"
                        "authority_sequence,effective_from,proof_snapshot_id,"
                        "proof_generation,promotion_request_id) VALUES ("
                        "'workspace_vm','kubevirt-vmis','cluster-a',$1,$2,"
                        "NULL,NULL,1,$3,$4,7,$5)",
                        scope_id,
                        recovery_epoch_id,
                        boundary,
                        snapshot_id,
                        rejected_request_id,
                    )

            request_id = uuid4()
            async with conn.transaction():
                promotion = await promote_compute_scope_epochs(
                    conn,
                    activation_key="workspace_vm",
                    activated_at=boundary,
                    source_cluster="cluster-a",
                    namespaces=("agent-vms",),
                    max_scope_age=timedelta(minutes=15),
                    expected_generation=7,
                    request_id=request_id,
                    actor_id=uuid4(),
                    audit_reason="healthy recovered VMI epoch proof",
                )
                activation = await schedule_compute_activation(
                    conn,
                    activation_key="workspace_vm",
                    activated_at=boundary,
                )
            assert not promotion.replayed
            assert len(promotion.authorities) == 1
            assert (
                promotion.authorities[0].inventory_scope_epoch_id == recovery_epoch_id
            )
            assert promotion.authorities[0].authority_sequence == 1
            assert activation.state == "active"
            assert activation.activated_at == boundary
    finally:
        await pool.close()


async def _insert_interval(
    conn: asyncpg.Connection,
    *,
    scope_id: UUID,
    source_kind: str,
    source_uid: str,
    resource: str,
    started_at: datetime,
    details: dict[str, object] | None = None,
    compute_scope_epoch_id: UUID | None = None,
    source_revision: str = "a" * 64,
) -> None:
    lifecycle_id = uuid4()
    is_vmi = source_kind == "vmi"
    measurement_basis = "guest-provisioned" if is_vmi else "scheduler-request"
    resource_class = "virtual-machine" if is_vmi else "kubernetes-pod"
    await conn.execute(
        "INSERT INTO resource_lifecycle_heads (source_lifecycle_id) VALUES ($1)",
        lifecycle_id,
    )
    await conn.execute(
        "INSERT INTO resource_intervals ("
        "inventory_scope_id,source_cluster,source_kind,source_uid,"
        "source_api_version,source_resource_version,source_lifecycle_id,"
        "revision_no,source_revision,namespace,name,category,resource,"
        "measurement_basis,cost_domain,resource_class,attribution_scope,"
        "owner_kind,owner_id,attribution_source,attribution_quality,"
        "lifecycle_confidence,cpu_millicores,memory_bytes,capacity_source,"
        "capacity_quality,measurement_algorithm,started_at,start_time_source,"
        "start_uncertainty_us,last_seen_at,last_confirmed_at,"
        "materialized_through,details,compute_scope_epoch_id) VALUES ("
        "$1,'cluster-a',$2,$3,$4,'rv-test',$5,1,$6,'workers',$3,"
        "'compute',$7,$8,'workload-allocation',$9,'shared-platform',"
        "'platform',NULL,'test-fixture','exact','kubernetes-visible',"
        "1000,1073741824,$8,'exact','test-v1',$10,'inventory-receipt',0,"
        "$11,$11,$10,$12::jsonb,$13)",
        scope_id,
        source_kind,
        source_uid,
        "kubevirt.io/v1" if is_vmi else "v1",
        lifecycle_id,
        source_revision,
        resource,
        measurement_basis,
        resource_class,
        started_at,
        started_at + timedelta(minutes=1),
        json.dumps(details or {}),
        compute_scope_epoch_id,
    )


async def _binding_state(
    conn: asyncpg.Connection,
    agent_id: UUID,
) -> asyncpg.Record:
    row = await conn.fetchrow(
        "SELECT * FROM agent_metering_pod_identity_state WHERE agent_id=$1",
        agent_id,
    )
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_compute_foundation_database_lifecycle(compute_pg_dsn: str) -> None:
    dbname = f"compute_foundation_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(compute_pg_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await admin.close()

    pool = await asyncpg.create_pool(
        _swap_db(compute_pg_dsn, dbname),
        min_size=1,
        max_size=6,
    )
    assert pool is not None
    try:
        await run_migrations(pool, APP_MIGRATIONS)
        # A restarted orchestrator must accept the immutable 0103 checksum and
        # treat the superseding 0104 function repair as already applied.
        await run_migrations(pool, APP_MIGRATIONS)
        store = ComputeActivationStore(pool)
        assert tuple(row.activation_key for row in await store.status()) == (
            "agent_pod",
            "ide_workspace_pod",
            "workspace_vm",
        )
        async with pool.acquire() as conn:
            migration_rows = await conn.fetch(
                "SELECT filename,success FROM schema_migrations WHERE filename "
                "IN ('0103_compute_metering_foundations.sql',"
                "'0104_agent_metering_lock_order.sql') ORDER BY filename"
            )
            assert [(row["filename"], row["success"]) for row in migration_rows] == [
                ("0103_compute_metering_foundations.sql", True),
                ("0104_agent_metering_lock_order.sql", True),
            ]
            agent_trigger_definition = await conn.fetchval(
                "SELECT pg_get_functiondef("
                "'converge_agent_metering_from_agent_row()'::regprocedure)"
            )
            job_trigger_definition = await conn.fetchval(
                "SELECT pg_get_functiondef("
                "'converge_agent_metering_from_job_row()'::regprocedure)"
            )
            thread_trigger_definition = await conn.fetchval(
                "SELECT pg_get_functiondef("
                "'converge_agent_metering_from_thread_row()'::regprocedure)"
            )
            assert "FOR NO KEY UPDATE" not in agent_trigger_definition
            assert "ORDER BY candidate.uid" in job_trigger_definition
            assert "ORDER BY candidate.uid" in thread_trigger_definition

            rows = await read_compute_activations(conn)
            assert tuple(row.activation_key for row in rows) == (
                "agent_pod",
                "ide_workspace_pod",
                "workspace_vm",
            )
            assert {row.state for row in rows} == {"disabled"}

            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE compute_metering_activation SET state='active',"
                        "activated_at=date_trunc('day',statement_timestamp(),'UTC') "
                        "WHERE activation_key='workspace_vm'"
                    )

            now = await conn.fetchval("SELECT statement_timestamp()")
            assert isinstance(now, datetime)
            boundary = (now + timedelta(days=1)).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            await advance_compute_activation_to_shadow(conn, "agent_pod")
            await advance_compute_activation_to_shadow(conn, "ide_workspace_pod")

            pod_scope_id, pod_epoch_id = uuid4(), uuid4()
            vm_scope_id = uuid4()
            await conn.execute(
                "UPDATE infra_metering_control SET leader_generation=7 WHERE singleton"
            )
            await conn.execute(
                "INSERT INTO resource_inventory_scopes ("
                "id,collector_id,source_cluster,api_resource,namespace) VALUES "
                "($1,'kubernetes-pods','cluster-a','core/v1/pods','workers'),"
                "($2,'kubevirt-vmis','cluster-a',"
                "'kubevirt.io/v1/virtualmachineinstances','workers')",
                pod_scope_id,
                vm_scope_id,
            )
            await conn.execute(
                "INSERT INTO resource_inventory_scope_epochs ("
                "id,scope_id,epoch_number,coverage_mode,leader_generation) "
                "VALUES ($1,$2,1,'list-watch',7)",
                pod_epoch_id,
                pod_scope_id,
            )

            inventory_store = InventoryStore(pool)
            await inventory_store.activate_generation(expected_generation=7)
            scope = InventoryScopeIdentity(
                collector_id="kubernetes-pods",
                source_cluster="cluster-a",
                api_resource="core/v1/pods",
                namespace="workers",
            )
            item = InventoryItem(
                source_kind="pod",
                source_uid="agent-pod-uid",
                revision_hash="d" * 64,
                normalized_item={
                    "source_kind": "pod",
                    "uid": "agent-pod-uid",
                    "namespace": "workers",
                },
                valid_for_metering=True,
            )
            ticket = await inventory_store.issue_ingest_ticket(
                pod_epoch_id,
                "c" * 64,
                scope=scope,
                transport=_transport(scope.collector_id, "snapshot-ticket"),
                max_snapshot_items=10,
                max_snapshot_bytes=100_000,
            )
            snapshot_id = uuid4()
            await inventory_store.begin_snapshot(
                ticket.token,
                ticket.id,
                snapshot_id,
                now - timedelta(minutes=2),
                scope=scope,
                transport=_transport(scope.collector_id, "snapshot-begin"),
            )
            await inventory_store.stage_items(
                ticket.token,
                ticket.id,
                snapshot_id,
                (item,),
                scope=scope,
                transport=_transport(scope.collector_id, "snapshot-items"),
            )
            observation_ids: list[UUID] = []

            async def write_agent_shadow(
                hook_conn: asyncpg.Connection,
                context: SnapshotObservationContext,
                observed_item: InventoryItem,
            ) -> None:
                observation_id = await hook_conn.fetchval(
                    "INSERT INTO compute_shadow_observations ("
                    "activation_key,snapshot_id,inventory_scope_id,source_kind,"
                    "source_uid,resource,product_class,cpu_millicores,memory_bytes,"
                    "attribution_scope,owner_kind,disposition,reason_code,"
                    "observed_at) VALUES ("
                    "'agent_pod',$1,$2,$3,$4,'agent_pod',"
                    "'agent-worker',1000,1073741824,'shared-platform','platform',"
                    "'eligible-unpriced','unbound-agent',$5) RETURNING id",
                    context.snapshot_id,
                    context.inventory_scope_id,
                    observed_item.source_kind,
                    observed_item.source_uid,
                    context.received_at,
                )
                assert isinstance(observation_id, UUID)
                observation_ids.append(observation_id)
                ide_observation_id = await hook_conn.fetchval(
                    "INSERT INTO compute_shadow_observations ("
                    "activation_key,snapshot_id,inventory_scope_id,source_kind,"
                    "source_uid,resource,product_class,attribution_scope,"
                    "disposition,reason_code,observed_at) VALUES ("
                    "'ide_workspace_pod',$1,$2,$3,$4,'workspace_pod',"
                    "'ide-session','unknown','not-applicable','not-ide-pod',$5) "
                    "RETURNING id",
                    context.snapshot_id,
                    context.inventory_scope_id,
                    observed_item.source_kind,
                    observed_item.source_uid,
                    context.received_at,
                )
                assert isinstance(ide_observation_id, UUID)
                observation_ids.append(ide_observation_id)

            await inventory_store.finalize_snapshot(
                ticket.token,
                ticket.id,
                snapshot_id,
                SnapshotFinalization(
                    collection_completed_at=now - timedelta(minutes=1),
                    complete=True,
                    item_count=1,
                    item_digest=inventory_manifest_digest((item,)),
                    resource_version="rv-agent-pod",
                ),
                scope=scope,
                transport=_transport(scope.collector_id, "snapshot-finalize"),
                observation_hook=write_agent_shadow,
                require_shadow_comparison=False,
                reconcile_intervals=False,
            )
            assert len(observation_ids) == 2
            for observation_id in observation_ids:
                for mutation in (
                    "UPDATE compute_shadow_observations SET reason_code='changed' "
                    "WHERE id=$1",
                    "DELETE FROM compute_shadow_observations WHERE id=$1",
                ):
                    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                        async with conn.transaction():
                            await conn.execute(mutation, observation_id)

            async with conn.transaction():
                await promote_compute_scope_epochs(
                    conn,
                    activation_key="agent_pod",
                    activated_at=boundary,
                    source_cluster="cluster-a",
                    namespaces=("workers",),
                    max_scope_age=timedelta(minutes=15),
                    expected_generation=7,
                    request_id=uuid4(),
                    actor_id=uuid4(),
                    audit_reason="database lifecycle activation proof",
                )
            first_requirement = await conn.fetchrow(
                "SELECT inventory_scope_id,inventory_scope_epoch_id,required_from "
                "FROM compute_metering_scope_requirements "
                "WHERE activation_key='agent_pod'"
            )
            assert first_requirement is not None
            assert (
                first_requirement["inventory_scope_id"],
                first_requirement["inventory_scope_epoch_id"],
                first_requirement["required_from"],
            ) == (pod_scope_id, pod_epoch_id, boundary)
            async with conn.transaction():
                await schedule_compute_activation(
                    conn,
                    activation_key="agent_pod",
                    activated_at=boundary,
                )
            async with conn.transaction():
                await promote_compute_scope_epochs(
                    conn,
                    activation_key="ide_workspace_pod",
                    activated_at=boundary,
                    source_cluster="cluster-a",
                    namespaces=("workers",),
                    max_scope_age=timedelta(minutes=15),
                    expected_generation=7,
                    request_id=uuid4(),
                    actor_id=uuid4(),
                    audit_reason="shared Pod exact authority proof",
                )
                await schedule_compute_activation(
                    conn,
                    activation_key="ide_workspace_pod",
                    activated_at=boundary,
                )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM resource_inventory_coverage_gaps "
                    "WHERE scope_epoch_id=$1 AND reason IN ("
                    "'compute-authority-awaiting-confirmation:agent_pod',"
                    "'compute-authority-awaiting-confirmation:ide_workspace_pod')",
                    pod_epoch_id,
                )
                == 2
            )
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE compute_metering_activation SET activated_at=$2 "
                        "WHERE activation_key=$1",
                        "agent_pod",
                        boundary + timedelta(days=1),
                    )

            await _insert_interval(
                conn,
                scope_id=pod_scope_id,
                source_kind="pod",
                source_uid="ordinary-workspace",
                resource="workspace_pod",
                started_at=now - timedelta(minutes=1),
            )
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await _insert_interval(
                        conn,
                        scope_id=pod_scope_id,
                        source_kind="pod",
                        source_uid="ordinary-workspace-with-compute-epoch",
                        resource="workspace_pod",
                        started_at=now - timedelta(minutes=1),
                        compute_scope_epoch_id=pod_epoch_id,
                    )
            guarded_intervals = (
                (
                    pod_scope_id,
                    "pod",
                    "agent-before-boundary",
                    "agent_pod",
                    {},
                ),
                (
                    pod_scope_id,
                    "pod",
                    "ide-before-boundary",
                    "workspace_pod",
                    {"product_class": "ide-session"},
                ),
                (
                    vm_scope_id,
                    "vmi",
                    "vm-before-boundary",
                    "workspace_vm",
                    {},
                ),
            )
            for (
                scope_id,
                source_kind,
                source_uid,
                resource,
                details,
            ) in guarded_intervals:
                with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                    async with conn.transaction():
                        await _insert_interval(
                            conn,
                            scope_id=scope_id,
                            source_kind=source_kind,
                            source_uid=source_uid,
                            resource=resource,
                            started_at=boundary,
                            details=details,
                        )

            # Move the immutable test authority behind us. Production
            # boundaries are one-way; this fixture uses trigger bypass solely
            # to exercise an already-crossed activation without waiting a day.
            crossed_boundary = (now - timedelta(days=1)).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            await conn.execute("SET session_replication_role = replica")
            try:
                await conn.execute(
                    "UPDATE compute_metering_activation SET activated_at=$2 "
                    "WHERE activation_key=$1",
                    "agent_pod",
                    crossed_boundary,
                )
                await conn.execute(
                    "UPDATE compute_metering_scope_requirements "
                    "SET required_from=$2 WHERE activation_key=$1",
                    "agent_pod",
                    crossed_boundary,
                )
                await conn.execute(
                    "UPDATE compute_metering_epoch_authorities "
                    "SET effective_from=$2 WHERE activation_key=$1",
                    "agent_pod",
                    crossed_boundary,
                )
                await conn.execute(
                    "UPDATE resource_inventory_coverage_gaps "
                    "SET gap_start=$2 WHERE reason="
                    "'compute-authority-awaiting-confirmation:agent_pod' "
                    "AND scope_epoch_id=$1",
                    pod_epoch_id,
                    crossed_boundary,
                )
                await conn.execute(
                    "UPDATE resource_inventory_scope_epochs SET "
                    "required_from=$2,reliable_from=$2,continuous_since=$2 "
                    "WHERE id=$1",
                    pod_epoch_id,
                    crossed_boundary,
                )
            finally:
                await conn.execute("SET session_replication_role = origin")

            await _insert_interval(
                conn,
                scope_id=pod_scope_id,
                source_kind="pod",
                source_uid="agent-promoted-epoch",
                resource="agent_pod",
                started_at=now - timedelta(minutes=2),
                compute_scope_epoch_id=pod_epoch_id,
            )

            # InventoryStore owns the scope epoch before invoking interval
            # reconciliation. Concurrent exact activation replay must wait on
            # that epoch without retaining the activation row and forming the
            # historical activation -> epoch / epoch -> activation deadlock.
            reconcile_has_epoch = asyncio.Event()
            release_reconcile = asyncio.Event()

            async def reconcile_while_holding_epoch() -> None:
                async with pool.acquire() as reconcile_conn:
                    async with reconcile_conn.transaction():
                        await reconcile_conn.fetchval(
                            "SELECT id FROM resource_inventory_scope_epochs "
                            "WHERE id=$1 FOR UPDATE",
                            pod_epoch_id,
                        )
                        reconcile_has_epoch.set()
                        await release_reconcile.wait()
                        await reconcile_conn.execute(
                            "UPDATE resource_intervals SET "
                            "last_seen_at=$1,last_confirmed_at=$1 "
                            "WHERE source_uid='agent-promoted-epoch'",
                            now,
                        )

            async def replay_activation() -> None:
                await reconcile_has_epoch.wait()
                async with pool.acquire() as activation_conn:
                    async with activation_conn.transaction():
                        replay = await schedule_compute_activation(
                            activation_conn,
                            activation_key="agent_pod",
                            activated_at=crossed_boundary,
                        )
                        assert replay.state == "active"

            reconcile_task = asyncio.create_task(reconcile_while_holding_epoch())
            activation_task = asyncio.create_task(replay_activation())
            await reconcile_has_epoch.wait()
            await asyncio.sleep(0.05)
            assert not activation_task.done()
            release_reconcile.set()
            await asyncio.wait_for(
                asyncio.gather(reconcile_task, activation_task),
                timeout=5,
            )

            # Epoch replacement must fail closed again even though the scope
            # identity and global class activation did not change.
            replacement_epoch_id = uuid4()
            retirement_at = await conn.fetchval("SELECT statement_timestamp()")
            await conn.execute(
                "UPDATE resource_inventory_scope_epochs SET retired_at=$2 WHERE id=$1",
                pod_epoch_id,
                retirement_at,
            )
            open_agent_id = await conn.fetchval(
                "SELECT id FROM resource_intervals "
                "WHERE source_uid='agent-promoted-epoch'"
            )
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE resource_intervals SET "
                        "last_seen_at=$2,last_confirmed_at=$2 "
                        "WHERE id=$1",
                        open_agent_id,
                        now + timedelta(minutes=3),
                    )
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE resource_intervals SET ended_at=$2,"
                        "end_time_source='app-db-received',end_uncertainty_us=120000000,"
                        "end_reason='epoch-rollover-test' WHERE id=$1",
                        open_agent_id,
                        now + timedelta(minutes=3),
                    )
            await conn.execute(
                "INSERT INTO resource_inventory_scope_epochs ("
                "id,scope_id,epoch_number,coverage_mode,leader_generation,"
                "recovery_from_epoch_id,require_after_recovery) "
                "VALUES ($1,$2,2,'list-watch',7,$3,TRUE)",
                replacement_epoch_id,
                pod_scope_id,
                pod_epoch_id,
            )
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                async with conn.transaction():
                    await _insert_interval(
                        conn,
                        scope_id=pod_scope_id,
                        source_kind="pod",
                        source_uid="agent-replacement-unpromoted",
                        resource="agent_pod",
                        started_at=now,
                        compute_scope_epoch_id=replacement_epoch_id,
                    )
            await conn.execute(
                "UPDATE resource_inventory_scope_epochs SET "
                "reliable_from=$2,required_for_rollup=TRUE,required_from=$2 "
                "WHERE id=$1",
                replacement_epoch_id,
                crossed_boundary,
            )
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                async with conn.transaction():
                    await _insert_interval(
                        conn,
                        scope_id=pod_scope_id,
                        source_kind="pod",
                        source_uid="agent-replacement-promoted",
                        resource="agent_pod",
                        started_at=now,
                        compute_scope_epoch_id=replacement_epoch_id,
                    )

            recovery_ticket = await inventory_store.issue_ingest_ticket(
                replacement_epoch_id,
                "e" * 64,
                scope=scope,
                transport=_transport(scope.collector_id, "snapshot-ticket"),
                max_snapshot_items=10,
                max_snapshot_bytes=100_000,
            )
            recovery_snapshot_id = uuid4()
            recovery_started_at = datetime.now(timezone.utc) - timedelta(seconds=2)
            await inventory_store.begin_snapshot(
                recovery_ticket.token,
                recovery_ticket.id,
                recovery_snapshot_id,
                recovery_started_at,
                scope=scope,
                transport=_transport(scope.collector_id, "snapshot-begin"),
            )
            await inventory_store.stage_items(
                recovery_ticket.token,
                recovery_ticket.id,
                recovery_snapshot_id,
                (item,),
                scope=scope,
                transport=_transport(scope.collector_id, "snapshot-items"),
            )
            await inventory_store.finalize_snapshot(
                recovery_ticket.token,
                recovery_ticket.id,
                recovery_snapshot_id,
                SnapshotFinalization(
                    collection_completed_at=datetime.now(timezone.utc),
                    complete=True,
                    item_count=1,
                    item_digest=inventory_manifest_digest((item,)),
                    resource_version="rv-agent-recovery",
                ),
                scope=scope,
                transport=_transport(scope.collector_id, "snapshot-finalize"),
                observation_hook=write_agent_shadow,
                require_shadow_comparison=False,
                reconcile_intervals=False,
            )
            rollover_request_id = uuid4()
            rollover = await promote_compute_recovery_epochs(
                conn,
                activation_key="agent_pod",
                source_cluster="cluster-a",
                namespaces=("workers",),
                max_scope_age=timedelta(minutes=15),
                expected_generation=7,
                request_id=rollover_request_id,
                actor_id=uuid4(),
                audit_reason="reviewed database recovery rollover",
            )
            assert not rollover.replayed
            assert len(rollover.authorities) == 1
            assert rollover.authorities[0].authority_sequence == 2
            assert (
                rollover.authorities[0].inventory_scope_epoch_id == replacement_epoch_id
            )
            rollover_gap = await conn.fetchrow(
                "SELECT gap_start,gap_end,resolution FROM "
                "resource_inventory_coverage_gaps WHERE scope_epoch_id=$1 "
                "AND reason="
                "'compute-authority-awaiting-confirmation:agent_pod'",
                replacement_epoch_id,
            )
            assert rollover_gap is not None
            assert rollover_gap["gap_start"] == retirement_at
            assert rollover_gap["gap_end"] is None
            assert rollover_gap["resolution"] == "unresolved"

            confirmation_ticket = await inventory_store.issue_ingest_ticket(
                replacement_epoch_id,
                "f" * 64,
                scope=scope,
                transport=_transport(scope.collector_id, "snapshot-ticket"),
                max_snapshot_items=10,
                max_snapshot_bytes=100_000,
            )
            confirmation_snapshot_id = uuid4()
            await inventory_store.begin_snapshot(
                confirmation_ticket.token,
                confirmation_ticket.id,
                confirmation_snapshot_id,
                datetime.now(timezone.utc) - timedelta(seconds=1),
                scope=scope,
                transport=_transport(scope.collector_id, "snapshot-begin"),
            )
            await inventory_store.stage_items(
                confirmation_ticket.token,
                confirmation_ticket.id,
                confirmation_snapshot_id,
                (item,),
                scope=scope,
                transport=_transport(scope.collector_id, "snapshot-items"),
            )
            confirmation_receipt = await conn.fetchval("SELECT statement_timestamp()")
            await _insert_interval(
                conn,
                scope_id=pod_scope_id,
                source_kind="pod",
                source_uid=item.source_uid,
                resource="agent_pod",
                started_at=confirmation_receipt,
                compute_scope_epoch_id=replacement_epoch_id,
                source_revision=item.revision_hash or "",
            )
            await conn.execute(
                "INSERT INTO compute_shadow_observations ("
                "activation_key,snapshot_id,inventory_scope_id,source_kind,"
                "source_uid,resource,product_class,cpu_millicores,memory_bytes,"
                "attribution_scope,owner_kind,disposition,reason_code,"
                "observed_at) VALUES ("
                "'agent_pod',$1,$2,'pod',$3,'agent_pod','agent-worker',"
                "1000,1073741824,'shared-platform','platform',"
                "'eligible-unpriced','unbound-agent',$4)",
                confirmation_snapshot_id,
                pod_scope_id,
                item.source_uid,
                confirmation_receipt,
            )
            assert (
                await confirm_compute_authority_snapshot(
                    conn,
                    activation_keys=("agent_pod",),
                    snapshot_id=confirmation_snapshot_id,
                    scope_epoch_id=pod_epoch_id,
                    inventory_scope_id=pod_scope_id,
                    received_at=confirmation_receipt,
                )
                == ()
            )
            assert await conn.fetchval(
                "SELECT gap_end IS NULL FROM resource_inventory_coverage_gaps "
                "WHERE scope_epoch_id=$1 AND reason="
                "'compute-authority-awaiting-confirmation:agent_pod'",
                replacement_epoch_id,
            )
            generation_mismatch = conn.transaction()
            await generation_mismatch.start()
            try:
                await conn.execute(
                    "UPDATE infra_metering_control SET leader_generation=8 "
                    "WHERE singleton=TRUE"
                )
                assert (
                    await confirm_compute_authority_snapshot(
                        conn,
                        activation_keys=("agent_pod",),
                        snapshot_id=confirmation_snapshot_id,
                        scope_epoch_id=replacement_epoch_id,
                        inventory_scope_id=pod_scope_id,
                        received_at=confirmation_receipt,
                    )
                    == ()
                )
                assert await conn.fetchval(
                    "SELECT gap_end IS NULL "
                    "FROM resource_inventory_coverage_gaps "
                    "WHERE scope_epoch_id=$1 AND reason="
                    "'compute-authority-awaiting-confirmation:agent_pod'",
                    replacement_epoch_id,
                )
            finally:
                await generation_mismatch.rollback()
            assert await confirm_compute_authority_snapshot(
                conn,
                activation_keys=("agent_pod",),
                snapshot_id=confirmation_snapshot_id,
                scope_epoch_id=replacement_epoch_id,
                inventory_scope_id=pod_scope_id,
                received_at=confirmation_receipt,
            ) == ("agent_pod",)
            assert await conn.fetchval(
                "SELECT gap_end=$2 FROM resource_inventory_coverage_gaps "
                "WHERE scope_epoch_id=$1 AND reason="
                "'compute-authority-awaiting-confirmation:agent_pod'",
                replacement_epoch_id,
                confirmation_receipt,
            )

            user_id, project_id = uuid4(), uuid4()
            agent_id, duplicate_agent_id, missing_agent_id = uuid4(), uuid4(), uuid4()
            await conn.execute(
                "INSERT INTO users (id,display_name,email) VALUES "
                "($1,'Compute owner',$2)",
                user_id,
                f"compute-{user_id}@example.test",
            )
            await conn.execute(
                "INSERT INTO projects (id,name) VALUES ($1,'Compute project')",
                project_id,
            )
            await conn.execute(
                "INSERT INTO agents (id,config_name,hostname,status,pod_uid) "
                "VALUES ($1,'worker','agent-a','ready','pod-uid-a')",
                agent_id,
            )
            state = await _binding_state(conn, agent_id)
            assert (state["identity_state"], state["attribution_scope"]) == (
                "valid",
                "shared-platform",
            )

            job_id = uuid4()
            await conn.execute(
                "INSERT INTO jobs ("
                "id,description,status,user_id,project_id,assigned_agent_id) "
                "VALUES ($1,'compute binding','processing',$2,$3,$4)",
                job_id,
                user_id,
                project_id,
                agent_id,
            )
            await conn.execute(
                "UPDATE agents SET current_job_id=$2 WHERE id=$1",
                agent_id,
                job_id,
            )
            state = await _binding_state(conn, agent_id)
            assert (
                state["attribution_scope"],
                state["owner_kind"],
                state["owner_id"],
                state["user_id"],
                state["project_id"],
            ) == ("customer", "job", job_id, user_id, project_id)

            await conn.execute("UPDATE jobs SET status='completed' WHERE id=$1", job_id)
            state = await _binding_state(conn, agent_id)
            assert state["attribution_scope"] == "unknown"
            assert state["reason_code"] == "job-binding-conflict"
            await conn.execute(
                "UPDATE agents SET current_job_id=NULL WHERE id=$1", agent_id
            )

            thread_id, thread_attach_token = uuid4(), uuid4()
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO threads ("
                    "id,title,user_id,project_id,agent_id,runtime_attach_token,"
                    "runtime_authority_exposed,status) "
                    "VALUES ($1,'Compute thread',$2,$3,$4,$5,true,'active')",
                    thread_id,
                    user_id,
                    project_id,
                    agent_id,
                    thread_attach_token,
                )
                await conn.execute(
                    "UPDATE agents SET thread_id=$2 WHERE id=$1",
                    agent_id,
                    thread_id,
                )
            state = await _binding_state(conn, agent_id)
            assert (
                state["attribution_scope"],
                state["owner_kind"],
                state["owner_id"],
            ) == ("customer", "thread", thread_id)
            await conn.execute(
                "UPDATE threads SET status='suspended' WHERE id=$1", thread_id
            )
            state = await _binding_state(conn, agent_id)
            assert state["attribution_scope"] == "unknown"
            assert state["reason_code"] == "thread-binding-conflict"
            async with conn.transaction():
                await conn.execute(
                    "UPDATE threads SET agent_id=NULL, runtime_attach_token=NULL "
                    "WHERE id=$1",
                    thread_id,
                )
                await conn.execute(
                    "UPDATE agents SET thread_id=NULL WHERE id=$1", agent_id
                )

            journal_count = await conn.fetchval(
                "SELECT count(*) FROM agent_metering_binding_events WHERE agent_id=$1",
                agent_id,
            )
            await conn.execute(
                "UPDATE agents SET hostname=hostname WHERE id=$1",
                agent_id,
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM agent_metering_binding_events "
                    "WHERE agent_id=$1",
                    agent_id,
                )
                == journal_count
            )

            await conn.execute(
                "INSERT INTO agents (id,config_name,hostname,status,pod_uid) "
                "VALUES ($1,'worker','agent-b','ready','pod-uid-a')",
                duplicate_agent_id,
            )
            for current_agent_id in (agent_id, duplicate_agent_id):
                state = await _binding_state(conn, current_agent_id)
                assert (
                    state["identity_state"],
                    state["attribution_scope"],
                    state["reason_code"],
                ) == ("duplicate", "unknown", "duplicate-pod-uid")

            await conn.execute(
                "UPDATE agents SET pod_uid='pod-uid-b' WHERE id=$1",
                duplicate_agent_id,
            )
            for current_agent_id in (agent_id, duplicate_agent_id):
                state = await _binding_state(conn, current_agent_id)
                assert (state["identity_state"], state["attribution_scope"]) == (
                    "valid",
                    "shared-platform",
                )

            # A peer row can already be tuple-locked before its AFTER trigger
            # waits for the shared Pod-identity advisory lock. Peer convergence
            # must therefore use a plain MVCC read: waiting on the peer tuple
            # deadlocks, while SKIP LOCKED leaves the survivor permanently
            # marked duplicate. Exercise both a move and a delete while the
            # surviving duplicate is locked by an unrelated transaction.
            async def assert_locked_survivor_converges(*, delete_peer: bool) -> None:
                survivor_id, departing_id = uuid4(), uuid4()
                shared_uid = f"pod-uid-locked-{uuid4()}"
                await conn.execute(
                    "INSERT INTO agents (id,config_name,hostname,status,pod_uid) "
                    "VALUES ($1,'worker','locked-survivor','ready',$3),"
                    "($2,'worker','locked-departing','ready',$3)",
                    survivor_id,
                    departing_id,
                    shared_uid,
                )
                for current_agent_id in (survivor_id, departing_id):
                    state = await _binding_state(conn, current_agent_id)
                    assert state["identity_state"] == "duplicate"

                async with pool.acquire() as locked_conn:
                    locked_transaction = locked_conn.transaction()
                    await locked_transaction.start()
                    try:
                        await locked_conn.fetchrow(
                            "SELECT id FROM agents WHERE id=$1 FOR UPDATE",
                            survivor_id,
                        )
                        async with pool.acquire() as departing_conn:
                            if delete_peer:
                                mutation = departing_conn.execute(
                                    "DELETE FROM agents WHERE id=$1",
                                    departing_id,
                                )
                            else:
                                mutation = departing_conn.execute(
                                    "UPDATE agents SET pod_uid=$2 WHERE id=$1",
                                    departing_id,
                                    f"{shared_uid}-departed",
                                )
                            await asyncio.wait_for(mutation, timeout=5)
                    finally:
                        await locked_transaction.rollback()

                survivor = await _binding_state(conn, survivor_id)
                assert (
                    survivor["identity_state"],
                    survivor["attribution_scope"],
                    survivor["reason_code"],
                ) == ("valid", "shared-platform", "unbound-agent")
                if delete_peer:
                    departed = await _binding_state(conn, departing_id)
                    assert not departed["agent_present"]
                    assert departed["identity_state"] == "missing"
                else:
                    departed = await _binding_state(conn, departing_id)
                    assert (
                        departed["identity_state"],
                        departed["attribution_scope"],
                    ) == ("valid", "shared-platform")

            await assert_locked_survivor_converges(delete_peer=False)
            await assert_locked_survivor_converges(delete_peer=True)

            async def wait_for_pod_advisory_lock(
                probe: asyncpg.Connection,
                pod_uid: str,
            ) -> None:
                lock_name = f"srw-agent-metering-pod:{pod_uid}"
                for _ in range(300):
                    acquired = await probe.fetchval(
                        "SELECT pg_try_advisory_lock(hashtextextended($1,0))",
                        lock_name,
                    )
                    if not acquired:
                        return
                    await probe.execute(
                        "SELECT pg_advisory_unlock(hashtextextended($1,0))",
                        lock_name,
                    )
                    await asyncio.sleep(0.01)
                pytest.fail(f"Pod advisory lock was not acquired for {pod_uid}")

            async def wait_for_advisory_waiter(
                probe: asyncpg.Connection,
                application_name: str,
            ) -> None:
                for _ in range(300):
                    waiting = await probe.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE application_name=$1 AND state='active' "
                        "AND wait_event_type='Lock')",
                        application_name,
                    )
                    if waiting:
                        return
                    await asyncio.sleep(0.01)
                pytest.fail(f"advisory waiter did not block: {application_name}")

            async def assert_owner_trigger_uses_canonical_uid_order(
                *,
                owner_kind: str,
                first_agent_id: UUID,
                second_agent_id: UUID,
                moving_agent_id: UUID,
            ) -> None:
                first_uid = f"z-{owner_kind}-uid"
                second_uid = f"a-{owner_kind}-uid"
                owner_id = uuid4()
                thread_handoff_token = uuid4()
                await conn.execute(
                    "INSERT INTO agents (id,config_name,hostname,status,pod_uid) "
                    "VALUES ($1,'worker',$4,'ready',$5),"
                    "($2,'worker',$6,'ready',$7),"
                    "($3,'worker',$8,'ready',$7)",
                    first_agent_id,
                    second_agent_id,
                    moving_agent_id,
                    f"{owner_kind}-first",
                    first_uid,
                    f"{owner_kind}-second",
                    second_uid,
                    f"{owner_kind}-moving",
                )
                if owner_kind == "job":
                    await conn.execute(
                        "INSERT INTO jobs (id,description,status,user_id,"
                        "assigned_agent_id) VALUES "
                        "($1,'lock-order job','created',$2,$3)",
                        owner_id,
                        user_id,
                        first_agent_id,
                    )
                    await conn.execute(
                        "UPDATE agents SET current_job_id=$3 WHERE id IN ($1,$2)",
                        first_agent_id,
                        second_agent_id,
                        owner_id,
                    )
                    # Flipping a pinned job to processing with an agent still
                    # attached is a dispatch boundary, so migration 0175's
                    # fence wants the authority marker in the same statement.
                    # The lock ordering under test is unaffected by it.
                    authority_sql = pinned_dispatch_authority_jsonb_sql(
                        agent_expr="assigned_agent_id",
                        lease_expr="lease_expires_at",
                    )
                    owner_mutation = f"""
                        UPDATE jobs
                           SET status='processing',
                               context = jsonb_set(
                                   COALESCE(context, '{{}}'::jsonb),
                                   '{{{WORKSPACE_DISPATCH_AUTHORITY_CONTEXT_KEY}}}',
                                   {authority_sql},
                                   true
                               )
                         WHERE id=$1
                    """
                else:
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO threads ("
                            "id,title,user_id,agent_id,runtime_attach_token,"
                            "runtime_authority_exposed,status) "
                            "VALUES ($1,'lock-order thread',$2,$3,$4,true,"
                            "'suspended')",
                            owner_id,
                            user_id,
                            first_agent_id,
                            uuid4(),
                        )
                        await conn.execute(
                            "UPDATE agents SET thread_id=$2 WHERE id=$1",
                            first_agent_id,
                            owner_id,
                        )
                    # A pinned thread may have only one reciprocal agent. The
                    # owner mutation therefore performs a real first->second
                    # handoff; the thread trigger still sees both Pod UIDs and
                    # exercises their canonical prelock order.
                    owner_mutation = (
                        "UPDATE threads SET status='active',agent_id=$2,"
                        "runtime_attach_token=$3 WHERE id=$1"
                    )

                blocker_name = f"srw-agent-metering-agent:{first_agent_id}"
                owner_application = f"metering-{owner_kind}-lock-order"
                mover_application = f"metering-{owner_kind}-agent-move"
                owner_task: asyncio.Task[str] | None = None
                mover_task: asyncio.Task[str] | None = None

                async def mutate_owner() -> str:
                    async with pool.acquire() as owner_conn:
                        await owner_conn.execute(
                            "SELECT set_config('application_name',$1,false)",
                            owner_application,
                        )
                        await owner_conn.execute("SET deadlock_timeout='100ms'")
                        if owner_kind == "thread":
                            async with owner_conn.transaction():
                                result = await owner_conn.execute(
                                    owner_mutation,
                                    owner_id,
                                    second_agent_id,
                                    thread_handoff_token,
                                )
                                await owner_conn.execute(
                                    "UPDATE agents SET thread_id=NULL WHERE id=$1",
                                    first_agent_id,
                                )
                                await owner_conn.execute(
                                    "UPDATE agents SET thread_id=$2 WHERE id=$1",
                                    second_agent_id,
                                    owner_id,
                                )
                            return result
                        return await owner_conn.execute(owner_mutation, owner_id)

                async def move_agent() -> str:
                    async with pool.acquire() as moving_conn:
                        await moving_conn.execute(
                            "SELECT set_config('application_name',$1,false)",
                            mover_application,
                        )
                        await moving_conn.execute("SET deadlock_timeout='100ms'")
                        return await moving_conn.execute(
                            "UPDATE agents SET pod_uid=$2 WHERE id=$1",
                            moving_agent_id,
                            first_uid,
                        )

                async with pool.acquire() as blocker, pool.acquire() as probe:
                    await blocker.execute(
                        "SELECT pg_advisory_lock(hashtextextended($1,0))",
                        blocker_name,
                    )
                    try:
                        owner_task = asyncio.create_task(mutate_owner())
                        await wait_for_pod_advisory_lock(probe, first_uid)
                        mover_task = asyncio.create_task(move_agent())
                        await wait_for_advisory_waiter(probe, mover_application)
                        await blocker.execute(
                            "SELECT pg_advisory_unlock(hashtextextended($1,0))",
                            blocker_name,
                        )
                        assert (
                            await asyncio.wait_for(owner_task, timeout=5) == "UPDATE 1"
                        )
                        assert (
                            await asyncio.wait_for(mover_task, timeout=5) == "UPDATE 1"
                        )
                    finally:
                        await blocker.execute(
                            "SELECT pg_advisory_unlock(hashtextextended($1,0))",
                            blocker_name,
                        )
                        for task in (owner_task, mover_task):
                            if task is not None and not task.done():
                                task.cancel()
                                with pytest.raises(asyncio.CancelledError):
                                    await task

            await assert_owner_trigger_uses_canonical_uid_order(
                owner_kind="job",
                first_agent_id=UUID("00000000-0000-0000-0000-000000000101"),
                second_agent_id=UUID("00000000-0000-0000-0000-000000000102"),
                moving_agent_id=UUID("00000000-0000-0000-0000-000000000103"),
            )
            await assert_owner_trigger_uses_canonical_uid_order(
                owner_kind="thread",
                first_agent_id=UUID("00000000-0000-0000-0000-000000000201"),
                second_agent_id=UUID("00000000-0000-0000-0000-000000000202"),
                moving_agent_id=UUID("00000000-0000-0000-0000-000000000203"),
            )

            race_agent_a, race_agent_b = uuid4(), uuid4()
            await conn.execute(
                "INSERT INTO agents (id,config_name,hostname,status,pod_uid) VALUES "
                "($1,'worker','race-a','ready','pod-uid-race'),"
                "($2,'worker','race-b','ready','pod-uid-race')",
                race_agent_a,
                race_agent_b,
            )

            async def move_racing_agent(agent: UUID, pod_uid: str) -> None:
                async with pool.acquire() as racing_conn:
                    async with racing_conn.transaction():
                        await racing_conn.execute(
                            "UPDATE agents SET pod_uid=$2 WHERE id=$1",
                            agent,
                            pod_uid,
                        )

            await asyncio.gather(
                move_racing_agent(race_agent_a, "pod-uid-race-a"),
                move_racing_agent(race_agent_b, "pod-uid-race-b"),
            )
            for current_agent_id in (race_agent_a, race_agent_b):
                state = await _binding_state(conn, current_agent_id)
                assert (state["identity_state"], state["attribution_scope"]) == (
                    "valid",
                    "shared-platform",
                )

            # Repeatedly contend on a shared Pod identity, then split again.
            # The waiter may have begun its statement first, so this catches
            # transition clocks that regress after advisory/row-lock waits.
            for race_round in range(8):
                shared_uid = f"pod-uid-race-shared-{race_round}"
                await asyncio.gather(
                    move_racing_agent(race_agent_a, shared_uid),
                    move_racing_agent(race_agent_b, shared_uid),
                )
                for current_agent_id in (race_agent_a, race_agent_b):
                    state = await _binding_state(conn, current_agent_id)
                    assert (
                        state["identity_state"],
                        state["attribution_scope"],
                    ) == ("duplicate", "unknown")

                await asyncio.gather(
                    move_racing_agent(
                        race_agent_a,
                        f"pod-uid-race-a-{race_round}",
                    ),
                    move_racing_agent(
                        race_agent_b,
                        f"pod-uid-race-b-{race_round}",
                    ),
                )
                for current_agent_id in (race_agent_a, race_agent_b):
                    state = await _binding_state(conn, current_agent_id)
                    assert (
                        state["identity_state"],
                        state["attribution_scope"],
                    ) == ("valid", "shared-platform")

            await conn.execute(
                "INSERT INTO agents (id,config_name,hostname,status,pod_uid) "
                "VALUES ($1,'worker','agent-missing','ready',NULL)",
                missing_agent_id,
            )
            missing = await _binding_state(conn, missing_agent_id)
            assert (
                missing["identity_state"],
                missing["attribution_scope"],
                missing["reason_code"],
            ) == ("missing", "unknown", "missing-pod-uid")

            await conn.execute("DELETE FROM agents WHERE id=$1", agent_id)
            tombstone = await _binding_state(conn, agent_id)
            assert not tombstone["agent_present"]
            assert tombstone["identity_state"] == "missing"
            assert tombstone["attribution_scope"] == "unknown"
            assert tombstone["reason_code"] == "agent-row-deleted"
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM agent_metering_binding_events "
                    "WHERE agent_id=$1",
                    agent_id,
                )
                == tombstone["revision"]
            )

            event_id = await conn.fetchval(
                "SELECT id FROM agent_metering_binding_events "
                "WHERE agent_id=$1 ORDER BY revision LIMIT 1",
                agent_id,
            )
            for mutation in (
                "UPDATE agent_metering_binding_events SET reason_code='changed' "
                "WHERE id=$1",
                "DELETE FROM agent_metering_binding_events WHERE id=$1",
            ):
                with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                    async with conn.transaction():
                        await conn.execute(mutation, event_id)
    finally:
        await pool.close()
