"""Slice 2 physical-storage identity and app-DB foundation contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from orchestrator.database.migrate import run_migrations
from orchestrator.services.infrastructure_metering.capabilities import (
    probe_schema_capabilities,
)
from orchestrator.services.infrastructure_metering.collectors.storage_normalization import (
    volume_identity_key_fingerprint as collector_key_fingerprint,
)
from orchestrator.services.infrastructure_metering.materializer import (
    InfrastructureUsageMaterializer,
    StoragePublicationAuthority,
    StoragePublicationPolicy,
)
from orchestrator.services.infrastructure_metering.queries import UsageVisibility
from orchestrator.services.infrastructure_metering.read_model import (
    SourceAwareUsageReadModel,
)
from orchestrator.services.infrastructure_metering.sealer import (
    DaySealDisposition,
    InfrastructureUsageDaySealer,
)
from orchestrator.services.infrastructure_metering.storage_assets import (
    StorageActivationNotReady,
    StorageAssetConflict,
    StorageAssetContractError,
    StorageSourceRequirementSpec,
    advance_storage_activation_to_shadow,
    advance_storage_source_activation_to_shadow,
    assert_backend_destroyed,
    derive_volume_asset_identity,
    ensure_volume_asset,
    list_backend_unverified_assets,
    lock_storage_activation,
    observe_volume_incarnation,
    open_backend_unverified_gap,
    promote_storage_scope_epochs,
    read_storage_asset_detail,
    register_storage_identity_key,
    schedule_storage_activation,
    schedule_storage_source_activation,
    volume_identity_key_fingerprint,
)
from orchestrator.services.infrastructure_metering.storage_mapping import (
    StorageResourceMappingRule,
    list_storage_resource_mapping_resources,
    register_storage_resource_mapping,
    resolve_storage_resource_mapping,
)
from orchestrator.services.usage_ledger import StrictUsagePublishResult


ROOT = Path(__file__).parents[1]
APP_MIGRATIONS = ROOT / "src" / "orchestrator" / "database" / "migrations" / "app"
STORAGE_MIGRATION = APP_MIGRATIONS / "0102_storage_asset_foundations.sql"
SOURCE_ACTIVATION_MIGRATION = APP_MIGRATIONS / "0105_storage_source_activation.sql"


def _asyncpg_dsn(url: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def _swap_db(dsn: str, dbname: str) -> str:
    head, _, tail = dsn.rpartition("/")
    query = ""
    if "?" in tail:
        query = "?" + tail.split("?", 1)[1]
    return f"{head}/{dbname}{query}"


@pytest.fixture(scope="module")
def storage_pg_dsn() -> str:
    testcontainers = pytest.importorskip("testcontainers.postgres")
    container = testcontainers.PostgresContainer("postgres:16")
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"no container runtime for storage migration test: {exc}")
    try:
        yield _asyncpg_dsn(container.get_connection_url())
    finally:
        container.stop()


def test_storage_migration_has_dark_launch_and_raw_free_contract() -> None:
    sql = STORAGE_MIGRATION.read_text()

    for table in (
        "storage_metering_activation",
        "storage_identity_key_state",
        "storage_volume_assets",
        "storage_volume_incarnations",
        "storage_asset_coverage_gaps",
        "storage_backend_assertions",
        "storage_shadow_observations",
    ):
        assert f"CREATE TABLE {table}" in sql

    assert "resource_intervals_storage_activation_guard" in sql
    assert "storage_backend_assertions_append_only" in sql
    assert "storage_backend_assertions_transition" in sql
    assert "storage_shadow_observations_immutable" in sql
    assert "disabled -> shadow -> future active" in sql
    assert "volume_handle" not in sql.lower()
    assert "volume_attributes" not in sql.lower()
    assert (
        "resource_interval_id"
        not in sql.split("CREATE TABLE storage_shadow_observations", 1)[1].split(
            ");", 1
        )[0]
    )


def test_source_activation_migration_is_exact_per_source_and_superseding() -> None:
    sql = SOURCE_ACTIVATION_MIGRATION.read_text()
    compact = " ".join(sql.split())

    assert "depends-on:    0104_agent_metering_lock_order.sql" in sql
    assert "CREATE TABLE storage_metering_source_activations" in sql
    assert "CREATE TABLE storage_metering_source_requirements" in sql
    assert (
        "PRIMARY KEY ( measurement_basis, collector_id, source_cluster, "
        "inventory_scope_id, requirement_role )" in compact
    )
    assert "requirement_role IN ('quantity', 'attribution')" in compact
    assert "resource_inventory_scopes_source_identity_uq" in sql
    assert "storage_metering_source_requirements_scope_idx" in sql
    assert "storage_metering_source_activations_one_way" in sql
    assert "storage_metering_source_requirements_immutable" in sql
    assert (
        "CREATE OR REPLACE FUNCTION enforce_resource_interval_storage_activation" in sql
    )
    assert "requirement.requirement_role = 'quantity'" in sql
    assert (
        "CREATE OR REPLACE FUNCTION protect_storage_shadow_observation_mutation" in sql
    )
    assert "storage shadow observation source fence failed" in sql
    assert "scope.collector_id = 'kubernetes-pods'" in sql
    assert "matching claim source must activate before volume source" in sql
    assert "global_boundary > NEW.activated_at" in sql
    assert "missing_shadow_count" not in sql  # proof is parameterized in runtime


def test_volume_asset_identity_is_stable_opaque_and_cluster_scoped() -> None:
    csi_digest = "a" * 64
    csi = derive_volume_asset_identity(
        source_cluster="cluster-a",
        normalized_source_uid=csi_digest,
        identity_scheme="csi-hmac-sha256-v1",
        identity_key_version="storage-v1",
        csi_driver="csi.example.test",
    )
    assert csi.asset_digest == csi_digest

    fallback = derive_volume_asset_identity(
        source_cluster="cluster-a",
        normalized_source_uid="pv-uid-123",
        identity_scheme="pv-uid-v1",
        identity_key_version="storage-v1",
        csi_driver=None,
    )
    fallback_replay = derive_volume_asset_identity(
        source_cluster="cluster-a",
        normalized_source_uid="pv-uid-123",
        identity_scheme="pv-uid-v1",
        identity_key_version="storage-v1",
        csi_driver=None,
    )
    other_cluster = derive_volume_asset_identity(
        source_cluster="cluster-b",
        normalized_source_uid="pv-uid-123",
        identity_scheme="pv-uid-v1",
        identity_key_version="storage-v1",
        csi_driver=None,
    )
    assert fallback.asset_digest == fallback_replay.asset_digest
    assert fallback.source_lifecycle_id == fallback_replay.source_lifecycle_id
    assert fallback.asset_digest != "pv-uid-123"
    assert fallback.asset_digest != other_cluster.asset_digest
    assert fallback.source_lifecycle_id != other_cluster.source_lifecycle_id

    with pytest.raises(StorageAssetContractError, match="opaque HMAC"):
        derive_volume_asset_identity(
            source_cluster="cluster-a",
            normalized_source_uid="provider/disk/raw-handle",
            identity_scheme="csi-hmac-sha256-v1",
            identity_key_version="storage-v1",
            csi_driver="csi.example.test",
        )


def test_identity_key_fingerprint_matches_collector_contract() -> None:
    key = b"test-only-storage-identity-key-material-123456789"
    fingerprint = volume_identity_key_fingerprint(key)
    assert fingerprint == collector_key_fingerprint(key)
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)


@pytest.mark.asyncio
async def test_storage_activation_lock_requires_exact_quantity_source_and_max_boundary() -> (
    None
):
    global_boundary = datetime(2026, 8, 6, tzinfo=timezone.utc)
    source_boundary = datetime(2026, 8, 7, tzinfo=timezone.utc)
    scope_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "global_state": "active",
        "global_activated_at": global_boundary,
        "source_state": "active",
        "source_activated_at": source_boundary,
        "database_time": source_boundary + timedelta(minutes=5),
    }

    assert (
        await lock_storage_activation(
            conn,
            measurement_basis="claim-requested",
            inventory_scope_id=scope_id,
            observed_started_at=global_boundary - timedelta(days=30),
        )
        == source_boundary
    )
    query, basis, locked_scope = conn.fetchrow.await_args.args
    assert "requirement.requirement_role = 'quantity'" in query
    assert (basis, locked_scope) == ("claim-requested", scope_id)

    conn.fetchrow.return_value = None
    with pytest.raises(StorageActivationNotReady, match="no source activation"):
        await lock_storage_activation(
            conn,
            measurement_basis="claim-requested",
            inventory_scope_id=uuid4(),
            observed_started_at=source_boundary,
        )


@pytest.mark.asyncio
async def test_source_shadow_resolves_exact_scope_and_advances_global_master() -> None:
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    scope_id = uuid4()
    source_disabled = {
        "measurement_basis": "claim-requested",
        "collector_id": "kubevirt-storage",
        "source_cluster": "vm-cluster",
        "state": "disabled",
        "activated_at": None,
        "database_time": now,
    }
    source_shadow = dict(source_disabled, state="shadow")
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {
            "measurement_basis": "claim-requested",
            "state": "disabled",
            "activated_at": None,
            "database_time": now,
        },
        {
            "measurement_basis": "claim-requested",
            "state": "shadow",
            "activated_at": None,
            "database_time": now,
        },
        {
            "id": scope_id,
            "api_resource": "core/v1/persistentvolumeclaims",
            "namespace": "agent-vms",
        },
        source_disabled,
        source_disabled,
        source_shadow,
        source_shadow,
    ]
    requirement_row = {
        "inventory_scope_id": scope_id,
        "requirement_role": "quantity",
        "api_resource": "core/v1/persistentvolumeclaims",
        "namespace": "agent-vms",
    }
    conn.fetch.side_effect = [[requirement_row], [requirement_row]]

    result = await advance_storage_source_activation_to_shadow(
        conn,
        measurement_basis="claim-requested",
        collector_id="kubevirt-storage",
        source_cluster="vm-cluster",
        requirements=(
            StorageSourceRequirementSpec(
                api_resource="core/v1/persistentvolumeclaims",
                namespace="agent-vms",
                requirement_role="quantity",
            ),
        ),
    )

    assert result.state == "shadow"
    assert result.requirements[0].inventory_scope_id == scope_id
    assert conn.fetchrow.await_args_list[2].args[1:] == (
        "kubevirt-storage",
        "vm-cluster",
        "core/v1/persistentvolumeclaims",
        "agent-vms",
    )
    inserted_requirement = conn.execute.await_args
    assert inserted_requirement.args[4:] == (scope_id, "quantity")

    duplicate = StorageSourceRequirementSpec(
        api_resource="core/v1/persistentvolumeclaims",
        namespace="agent-vms",
        requirement_role="quantity",
    )
    with pytest.raises(StorageAssetContractError, match="duplicates"):
        await advance_storage_source_activation_to_shadow(
            AsyncMock(),
            measurement_basis="claim-requested",
            collector_id="kubevirt-storage",
            source_cluster="vm-cluster",
            requirements=(duplicate, duplicate),
        )


@pytest.mark.asyncio
async def test_source_schedule_proves_item_identity_and_promotes_every_scope() -> None:
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    boundary = datetime(2026, 8, 7, tzinfo=timezone.utc)
    scope_id = uuid4()
    epoch_id = uuid4()
    requirement = {
        "inventory_scope_id": scope_id,
        "requirement_role": "quantity",
        "api_resource": "core/v1/persistentvolumeclaims",
        "namespace": "agent-vms",
    }
    source_state = "shadow"
    global_state = "shadow"

    async def fetchrow(query, *args):  # noqa: ANN001
        nonlocal source_state, global_state
        if "storage-assets:lock-source-activation" in query:
            return {
                "measurement_basis": "claim-requested",
                "collector_id": "kubevirt-storage",
                "source_cluster": "vm-cluster",
                "state": source_state,
                "activated_at": boundary if source_state == "active" else None,
                "database_time": now,
            }
        if "storage-assets:lock-activation" in query:
            return {
                "measurement_basis": "claim-requested",
                "state": global_state,
                "activated_at": boundary if global_state == "active" else None,
                "database_time": now,
            }
        if query.startswith("SELECT measurement_basis,state,activated_at"):
            return {
                "measurement_basis": "claim-requested",
                "state": global_state,
                "activated_at": None,
                "database_time": now,
            }
        if query.startswith("UPDATE storage_metering_activation"):
            global_state = "active"
            return {
                "measurement_basis": "claim-requested",
                "state": "active",
                "activated_at": boundary,
                "database_time": now,
            }
        if query.startswith("UPDATE storage_metering_source_activations"):
            assert args == (
                "claim-requested",
                "kubevirt-storage",
                "vm-cluster",
                boundary,
            )
            source_state = "active"
            return {
                "measurement_basis": "claim-requested",
                "collector_id": "kubevirt-storage",
                "source_cluster": "vm-cluster",
                "state": "active",
                "activated_at": boundary,
                "database_time": now,
            }
        raise AssertionError(query)

    proof = {
        **requirement,
        "epoch_id": epoch_id,
        "required_for_rollup": False,
        "required_from": None,
        "reliable_from": boundary - timedelta(hours=2),
        "continuous_since": boundary - timedelta(hours=2),
        "last_complete_at": now - timedelta(minutes=1),
        "snapshot_health": "healthy",
        "item_health": "healthy",
        "continuity_health": "healthy",
        "backend_health": "healthy",
        "leader_generation": 7,
        "snapshot_leader_generation": 7,
        "item_count": 1,
        "complete": True,
        "manifest_state": "sealed",
        "received_at": now - timedelta(minutes=1),
        "shadow_count": 1,
        "missing_shadow_count": 0,
        "orphan_shadow_count": 0,
    }
    conn = AsyncMock()
    conn.fetchrow.side_effect = fetchrow

    async def fetch(query, *_args):  # noqa: ANN001
        if "epoch.id AS epoch_id" in query:
            return [proof]
        if "storage-assets:read-source-requirements" in query:
            return [requirement]
        raise AssertionError(query)

    conn.fetch.side_effect = fetch

    async def fetchval(query, *args):  # noqa: ANN001
        if "leader_generation FROM infra_metering_control" in query:
            return 7
        if query == "SELECT statement_timestamp()":
            return now
        if query.startswith("UPDATE resource_inventory_scope_epochs"):
            assert args == (epoch_id, boundary)
            return True
        raise AssertionError(query)

    conn.fetchval.side_effect = fetchval
    result = await schedule_storage_source_activation(
        conn,
        measurement_basis="claim-requested",
        collector_id="kubevirt-storage",
        source_cluster="vm-cluster",
        activated_at=boundary,
        max_scope_age=timedelta(minutes=15),
        expected_generation=7,
    )

    assert result.state == "active"
    proof_query = next(
        call.args[0]
        for call in conn.fetch.await_args_list
        if "epoch.id AS epoch_id" in call.args[0]
    )
    assert "missing_shadow_count" in proof_query
    assert "orphan_shadow_count" in proof_query


@pytest.mark.asyncio
async def test_source_schedule_accepts_an_already_required_earlier_boundary() -> None:
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    boundary = datetime(2026, 8, 7, tzinfo=timezone.utc)
    scope_id = uuid4()
    requirement = {
        "inventory_scope_id": scope_id,
        "requirement_role": "quantity",
        "api_resource": "core/v1/persistentvolumeclaims",
        "namespace": "agent-vms",
    }
    conn = AsyncMock()
    source_reads = 0

    async def fetchrow(query, *_args):  # noqa: ANN001
        nonlocal source_reads
        if "storage-assets:lock-source-activation" in query:
            source_reads += 1
            return {
                "measurement_basis": "claim-requested",
                "collector_id": "kubevirt-storage",
                "source_cluster": "vm-cluster",
                "state": "shadow" if source_reads == 1 else "active",
                "activated_at": None if source_reads == 1 else boundary,
                "database_time": now,
            }
        if "storage-assets:lock-activation" in query:
            return {
                "measurement_basis": "claim-requested",
                "state": "active",
                "activated_at": boundary - timedelta(days=2),
                "database_time": now,
            }
        if query.startswith("UPDATE storage_metering_source_activations"):
            return {
                "measurement_basis": "claim-requested",
                "collector_id": "kubevirt-storage",
                "source_cluster": "vm-cluster",
                "state": "active",
                "activated_at": boundary,
                "database_time": now,
            }
        raise AssertionError(query)

    conn.fetchrow.side_effect = fetchrow
    conn.fetch.side_effect = lambda query, *_args: (
        [
            {
                **requirement,
                "epoch_id": uuid4(),
                "required_for_rollup": True,
                "required_from": boundary - timedelta(days=1),
                "reliable_from": boundary - timedelta(days=2),
                "continuous_since": boundary - timedelta(days=2),
                "last_complete_at": now - timedelta(minutes=1),
                "snapshot_health": "healthy",
                "item_health": "healthy",
                "continuity_health": "healthy",
                "backend_health": "healthy",
                "leader_generation": 7,
                "snapshot_leader_generation": 7,
                "item_count": 0,
                "complete": True,
                "manifest_state": "sealed",
                "received_at": now - timedelta(minutes=1),
                "shadow_count": 0,
                "missing_shadow_count": 0,
                "orphan_shadow_count": 0,
            }
        ]
        if "epoch.id AS epoch_id" in query
        else [requirement]
    )
    conn.fetchval.side_effect = lambda query, *_args: (
        7 if "leader_generation FROM infra_metering_control" in query else now
    )

    result = await schedule_storage_source_activation(
        conn,
        measurement_basis="claim-requested",
        collector_id="kubevirt-storage",
        source_cluster="vm-cluster",
        activated_at=boundary,
        max_scope_age=timedelta(minutes=15),
        expected_generation=7,
    )
    assert result.state == "active"
    assert not any(
        call.args[0].startswith("UPDATE resource_inventory_scope_epochs")
        for call in conn.fetchval.await_args_list
    )


@pytest.mark.asyncio
async def test_storage_scope_promotion_requires_fresh_complete_shadow_proof() -> None:
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    boundary = datetime(2026, 8, 7, tzinfo=timezone.utc)
    epoch_id = uuid4()
    healthy = {
        "id": epoch_id,
        "namespace": "srw",
        "required_for_rollup": False,
        "required_from": None,
        "reliable_from": now - timedelta(hours=1),
        "continuous_since": now - timedelta(hours=1),
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
    }
    conn = AsyncMock()
    conn.fetch.return_value = [healthy]

    async def fetchval(query, *args):  # noqa: ANN001
        if "leader_generation FROM infra_metering_control" in query:
            return 7
        if "statement_timestamp" in query and not query.startswith("UPDATE"):
            return now
        if query.startswith("UPDATE resource_inventory_scope_epochs"):
            assert args == (epoch_id, boundary)
            return True
        raise AssertionError(query)

    conn.fetchval.side_effect = fetchval
    await promote_storage_scope_epochs(
        conn,
        measurement_basis="claim-requested",
        activated_at=boundary,
        source_cluster="cluster-a",
        namespaces=("srw",),
        max_scope_age=timedelta(minutes=15),
        expected_generation=7,
    )

    stale = dict(healthy, last_complete_at=now - timedelta(hours=1))
    stale_conn = AsyncMock()
    stale_conn.fetch.return_value = [stale]

    async def stale_fetchval(query, *_args):  # noqa: ANN001
        return 7 if "leader_generation FROM infra_metering_control" in query else now

    stale_conn.fetchval.side_effect = stale_fetchval
    with pytest.raises(StorageAssetConflict, match="fresh continuous shadow"):
        await promote_storage_scope_epochs(
            stale_conn,
            measurement_basis="claim-requested",
            activated_at=boundary,
            source_cluster="cluster-a",
            namespaces=("srw",),
            max_scope_age=timedelta(minutes=15),
            expected_generation=7,
        )

    generation_conn = AsyncMock()
    generation_conn.fetch.return_value = [healthy]

    async def generation_fetchval(query, *_args):  # noqa: ANN001
        return 8 if "leader_generation FROM infra_metering_control" in query else now

    generation_conn.fetchval.side_effect = generation_fetchval
    with pytest.raises(StorageAssetConflict, match="another collector generation"):
        await promote_storage_scope_epochs(
            generation_conn,
            measurement_basis="claim-requested",
            activated_at=boundary,
            source_cluster="cluster-a",
            namespaces=("srw",),
            max_scope_age=timedelta(minutes=15),
            expected_generation=7,
        )


@pytest.mark.asyncio
async def test_storage_activation_rejects_nonfuture_boundary_but_replays_exactly() -> (
    None
):
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    midnight = now.replace(hour=0)
    conn = AsyncMock()

    with pytest.raises(StorageAssetContractError, match="UTC midnight"):
        await schedule_storage_activation(
            conn,
            measurement_basis="claim-requested",
            activated_at=now,
        )
    conn.fetchrow.assert_not_awaited()

    conn.fetchrow.return_value = {
        "measurement_basis": "claim-requested",
        "state": "shadow",
        "activated_at": None,
        "database_time": now,
    }
    with pytest.raises(StorageAssetContractError, match="future UTC midnight"):
        await schedule_storage_activation(
            conn,
            measurement_basis="claim-requested",
            activated_at=midnight,
        )

    conn.fetchrow.return_value = {
        "measurement_basis": "claim-requested",
        "state": "active",
        "activated_at": midnight,
        "database_time": now,
    }
    replay = await schedule_storage_activation(
        conn,
        measurement_basis="claim-requested",
        activated_at=midnight,
    )
    assert replay.state == "active"
    assert replay.activated_at == midnight


@pytest.mark.asyncio
async def test_0105_backfills_active_primary_sources_without_moving_earlier_scopes(
    storage_pg_dsn: str,
    tmp_path: Path,
) -> None:
    dbname = f"storage_source_backfill_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(storage_pg_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await admin.close()

    pre_0105 = tmp_path / "pre-0105"
    through_0105 = tmp_path / "through-0105"
    pre_0105.mkdir()
    through_0105.mkdir()
    for migration in APP_MIGRATIONS.iterdir():
        if migration.is_file() and migration.name < SOURCE_ACTIVATION_MIGRATION.name:
            (pre_0105 / migration.name).symlink_to(migration.resolve())
        if migration.is_file() and migration.name <= SOURCE_ACTIVATION_MIGRATION.name:
            (through_0105 / migration.name).symlink_to(migration.resolve())

    pool = await asyncpg.create_pool(
        _swap_db(storage_pg_dsn, dbname),
        min_size=1,
        max_size=2,
    )
    assert pool is not None
    try:
        await run_migrations(pool, pre_0105)
        boundary = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        earlier = boundary - timedelta(days=1)
        claim_scope_id = uuid4()
        volume_scope_id = uuid4()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for basis in ("claim-requested", "volume-provisioned"):
                    await advance_storage_activation_to_shadow(conn, basis)
                    await schedule_storage_activation(
                        conn,
                        measurement_basis=basis,
                        activated_at=boundary,
                    )
                await conn.executemany(
                    "INSERT INTO resource_inventory_scopes ("
                    "id,collector_id,source_cluster,api_resource,namespace"
                    ") VALUES ($1,'kubernetes-pods','cluster-a',$2,$3)",
                    [
                        (
                            claim_scope_id,
                            "core/v1/persistentvolumeclaims",
                            "srw",
                        ),
                        (
                            volume_scope_id,
                            "core/v1/persistentvolumes",
                            None,
                        ),
                    ],
                )
                await conn.executemany(
                    "INSERT INTO resource_inventory_scope_epochs ("
                    "scope_id,epoch_number,reliable_from,required_for_rollup,"
                    "required_from,coverage_mode"
                    ") VALUES ($1,1,$2,TRUE,$3,'list-watch')",
                    [
                        (claim_scope_id, earlier - timedelta(days=1), earlier),
                        (volume_scope_id, earlier - timedelta(days=1), earlier),
                    ],
                )

        await run_migrations(pool, through_0105)
        await run_migrations(pool, through_0105)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT measurement_basis,state,activated_at "
                "FROM storage_metering_source_activations "
                "WHERE collector_id='kubernetes-pods' "
                "AND source_cluster='cluster-a' ORDER BY measurement_basis"
            )
            assert [row["measurement_basis"] for row in rows] == [
                "claim-requested",
                "volume-provisioned",
            ]
            assert all(row["state"] == "active" for row in rows)
            assert all(row["activated_at"] == boundary for row in rows)
            requirements = await conn.fetch(
                "SELECT measurement_basis,inventory_scope_id,requirement_role "
                "FROM storage_metering_source_requirements "
                "ORDER BY measurement_basis,requirement_role"
            )
            assert {
                (
                    row["measurement_basis"],
                    row["inventory_scope_id"],
                    row["requirement_role"],
                )
                for row in requirements
            } == {
                ("claim-requested", claim_scope_id, "quantity"),
                ("volume-provisioned", claim_scope_id, "attribution"),
                ("volume-provisioned", volume_scope_id, "quantity"),
            }
    finally:
        await pool.close()
        admin = await asyncpg.connect(storage_pg_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            await admin.close()


@pytest.mark.asyncio
async def test_storage_foundation_lifecycle_and_capabilities(
    storage_pg_dsn: str,
) -> None:
    dbname = f"storage_assets_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(storage_pg_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await admin.close()

    dsn = _swap_db(storage_pg_dsn, dbname)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    assert pool is not None
    try:
        await run_migrations(pool, APP_MIGRATIONS)
        capabilities = await probe_schema_capabilities(pool, None)
        assert capabilities.slice2_claim_inventory_ready
        assert capabilities.slice2_volume_schema_ready
        assert not capabilities.slice2_volume_inventory_ready
        assert capabilities.storage_identity_key_version is None
        assert not capabilities.missing_slice2_claim_app_constraints
        assert not capabilities.missing_slice2_volume_app_constraints

        mapping_rule = StorageResourceMappingRule(
            source_cluster="cluster-a",
            storage_class_name="standard",
            csi_driver="csi.example.test",
            volume_mode="filesystem",
            resource="block_volume_test_standard",
            mapping_version="test-standard-v1",
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                created_mapping = await register_storage_resource_mapping(
                    conn, mapping_rule
                )
                replayed_mapping = await register_storage_resource_mapping(
                    conn, mapping_rule
                )
                assert not created_mapping.replayed
                assert replayed_mapping.replayed
                resolved_mapping = await resolve_storage_resource_mapping(
                    conn, mapping_rule.key
                )
                assert resolved_mapping.resource == mapping_rule.resource
                assert await list_storage_resource_mapping_resources(conn) == (
                    mapping_rule.resource,
                )
        async with pool.acquire() as conn:
            async with conn.transaction():
                with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                    await conn.execute(
                        "UPDATE infrastructure_storage_resource_mappings "
                        "SET mapping_version='changed' WHERE rule_fingerprint=$1",
                        mapping_rule.fingerprint,
                    )

        now = datetime.now(timezone.utc).replace(microsecond=0)
        key_fingerprint = "b" * 64
        identity = derive_volume_asset_identity(
            source_cluster="cluster-a",
            normalized_source_uid="c" * 64,
            identity_scheme="csi-hmac-sha256-v1",
            identity_key_version="storage-v1",
            csi_driver="csi.example.test",
        )

        async with pool.acquire() as conn:
            async with conn.transaction():
                assert not await register_storage_identity_key(
                    conn,
                    key_version="storage-v1",
                    key_fingerprint=key_fingerprint,
                )
                assert await register_storage_identity_key(
                    conn,
                    key_version="storage-v1",
                    key_fingerprint=key_fingerprint,
                )
                with pytest.raises(StorageAssetConflict, match="fingerprint changed"):
                    await register_storage_identity_key(
                        conn,
                        key_version="storage-v1",
                        key_fingerprint="d" * 64,
                    )

        capabilities = await probe_schema_capabilities(pool, None)
        assert capabilities.slice2_volume_inventory_ready
        assert capabilities.storage_identity_key_version == "storage-v1"

        scope_id = uuid4()
        epoch_id = uuid4()
        claim_scope_id = uuid4()
        claim_epoch_id = uuid4()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO resource_inventory_scopes ("
                    "id, collector_id, source_cluster, api_resource, namespace) "
                    "VALUES ($1, 'kubernetes-storage', 'cluster-a', "
                    "'core/v1/persistentvolumes', NULL)",
                    scope_id,
                )
                await conn.execute(
                    "INSERT INTO resource_inventory_scope_epochs ("
                    "id, scope_id, epoch_number, coverage_mode) "
                    "VALUES ($1, $2, 1, 'list-watch')",
                    epoch_id,
                    scope_id,
                )
                await conn.execute(
                    "INSERT INTO resource_inventory_scopes ("
                    "id, collector_id, source_cluster, api_resource, namespace) "
                    "VALUES ($1, 'kubernetes-storage', 'cluster-a', "
                    "'core/v1/persistentvolumeclaims', 'srw')",
                    claim_scope_id,
                )
                await conn.execute(
                    "INSERT INTO resource_inventory_scope_epochs ("
                    "id, scope_id, epoch_number, coverage_mode) "
                    "VALUES ($1, $2, 1, 'list-watch')",
                    claim_epoch_id,
                    claim_scope_id,
                )

                claim_source = await advance_storage_source_activation_to_shadow(
                    conn,
                    measurement_basis="claim-requested",
                    collector_id="kubernetes-storage",
                    source_cluster="cluster-a",
                    requirements=(
                        StorageSourceRequirementSpec(
                            api_resource="core/v1/persistentvolumeclaims",
                            namespace="srw",
                            requirement_role="quantity",
                        ),
                    ),
                )
                assert claim_source.state == "shadow"
                assert claim_source.requirements[0].inventory_scope_id == claim_scope_id
                claim_replay = await advance_storage_source_activation_to_shadow(
                    conn,
                    measurement_basis="claim-requested",
                    collector_id="kubernetes-storage",
                    source_cluster="cluster-a",
                    requirements=(
                        StorageSourceRequirementSpec(
                            api_resource="core/v1/persistentvolumeclaims",
                            namespace="srw",
                            requirement_role="quantity",
                        ),
                    ),
                )
                assert claim_replay.state == "shadow"

                volume_source = await advance_storage_source_activation_to_shadow(
                    conn,
                    measurement_basis="volume-provisioned",
                    collector_id="kubernetes-storage",
                    source_cluster="cluster-a",
                    requirements=(
                        StorageSourceRequirementSpec(
                            api_resource="core/v1/persistentvolumes",
                            namespace=None,
                            requirement_role="quantity",
                        ),
                        StorageSourceRequirementSpec(
                            api_resource="core/v1/persistentvolumeclaims",
                            namespace="srw",
                            requirement_role="attribution",
                        ),
                    ),
                )
                assert volume_source.state == "shadow"
                assert {
                    (row.inventory_scope_id, row.requirement_role)
                    for row in volume_source.requirements
                } == {
                    (scope_id, "quantity"),
                    (claim_scope_id, "attribution"),
                }
                with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                    async with conn.transaction():
                        await conn.execute(
                            "DELETE FROM storage_metering_source_requirements "
                            "WHERE measurement_basis='claim-requested' "
                            "AND collector_id='kubernetes-storage' "
                            "AND source_cluster='cluster-a'"
                        )

                asset = await ensure_volume_asset(
                    conn,
                    identity=identity,
                    observed_at=now - timedelta(minutes=10),
                )
                replay = await ensure_volume_asset(
                    conn,
                    identity=identity,
                    observed_at=now - timedelta(minutes=9),
                )
                assert replay.id == asset.id
                assert replay.replayed

                incarnation = await observe_volume_incarnation(
                    conn,
                    asset_id=asset.id,
                    inventory_scope_id=scope_id,
                    source_cluster="cluster-a",
                    pv_uid="pv-object-uid-1",
                    pv_name="pv-test-1",
                    storage_class_name="standard",
                    reclaim_policy="retain",
                    backend_deletion_finalizer_observed=True,
                    volume_mode="filesystem",
                    # Kubernetes Quantity normalization accepts zero; keep the
                    # persistence boundary aligned instead of rejecting it.
                    capacity_bytes=0,
                    bound_claim_uid="claim-uid-1",
                    source_resource_version="10",
                    observed_at=now - timedelta(minutes=9),
                )
                assert not incarnation.replayed

                with pytest.raises(
                    StorageAssetConflict, match="detached with one backend"
                ):
                    await assert_backend_destroyed(
                        conn,
                        idempotency_key=uuid4(),
                        asset_id=asset.id,
                        effective_at=now - timedelta(minutes=1),
                        evidence_kind="provider-confirmed",
                        evidence_digest="f" * 64,
                        actor_kind="service",
                        actor_id=None,
                        reason_code="premature-destruction",
                    )
                await conn.execute(
                    "UPDATE storage_volume_incarnations SET detached_at=$2,"
                    "detach_reason='pv-deleted',updated_at=statement_timestamp() "
                    "WHERE id=$1",
                    incarnation.id,
                    now - timedelta(minutes=5),
                )

                gap = await open_backend_unverified_gap(
                    conn,
                    asset_id=asset.id,
                    scope_epoch_id=epoch_id,
                    gap_start=now - timedelta(minutes=5),
                    reason_code="retain-pv-absent",
                )
                assert not gap.replayed
                gap_replay = await open_backend_unverified_gap(
                    conn,
                    asset_id=asset.id,
                    scope_epoch_id=epoch_id,
                    gap_start=now - timedelta(minutes=5),
                    reason_code="retain-pv-absent",
                )
                assert gap_replay.id == gap.id
                assert gap_replay.replayed
                assert (
                    await conn.fetchval(
                        "SELECT lifecycle_state FROM storage_volume_assets "
                        "WHERE id = $1",
                        asset.id,
                    )
                    == "backend-unverified"
                )
                unverified = await list_backend_unverified_assets(conn, limit=10)
                assert unverified.next_cursor is None
                assert len(unverified.items) == 1
                assert unverified.items[0].asset_id == asset.id
                assert unverified.items[0].capacity_bytes == 0
                assert unverified.items[0].reason_code == "retain-pv-absent"
                detail = await read_storage_asset_detail(
                    conn,
                    asset_id=asset.id,
                    history_limit=10,
                )
                assert detail.lifecycle_state == "backend-unverified"
                assert detail.incarnations[0].storage_class_name == "standard"
                assert detail.gaps[0].gap_id == gap.id
                assert not detail.assertions

                request_id = uuid4()
                assertion = await assert_backend_destroyed(
                    conn,
                    idempotency_key=request_id,
                    asset_id=asset.id,
                    effective_at=now - timedelta(minutes=1),
                    evidence_kind="provider-confirmed",
                    evidence_digest="e" * 64,
                    actor_kind="service",
                    actor_id=None,
                    reason_code="provider-delete-confirmed",
                )
                assert not assertion.replayed
                assertion_replay = await assert_backend_destroyed(
                    conn,
                    idempotency_key=request_id,
                    asset_id=asset.id,
                    effective_at=now - timedelta(minutes=1),
                    evidence_kind="provider-confirmed",
                    evidence_digest="e" * 64,
                    actor_kind="service",
                    actor_id=None,
                    reason_code="provider-delete-confirmed",
                )
                assert assertion_replay.assertion_id == assertion.assertion_id
                assert assertion_replay.replayed

                state = await conn.fetchrow(
                    "SELECT lifecycle_state, destroyed_at, "
                    "destruction_assertion_id FROM storage_volume_assets "
                    "WHERE id = $1",
                    asset.id,
                )
                assert state is not None
                assert state["lifecycle_state"] == "destroyed"
                assert state["destruction_assertion_id"] == assertion.assertion_id
                assert (
                    await conn.fetchval(
                        "SELECT resolution FROM storage_asset_coverage_gaps "
                        "WHERE id = $1",
                        gap.id,
                    )
                    == "destroyed-confirmed"
                )
                assert (
                    await conn.fetchval(
                        "SELECT detach_reason FROM storage_volume_incarnations "
                        "WHERE id = $1",
                        incarnation.id,
                    )
                    == "pv-deleted"
                )
                assert not (await list_backend_unverified_assets(conn, limit=10)).items
                destroyed = await read_storage_asset_detail(
                    conn,
                    asset_id=asset.id,
                    history_limit=10,
                )
                assert destroyed.lifecycle_state == "destroyed"
                assert destroyed.assertions[0].assertion_id == assertion.assertion_id

        # Activation is independent by basis and always scheduled in the future.
        tomorrow = (now + timedelta(days=2)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if tomorrow <= now:
            tomorrow += timedelta(days=1)
        async with pool.acquire() as conn:
            async with conn.transaction():
                shadow = await advance_storage_activation_to_shadow(
                    conn, "claim-requested"
                )
                assert shadow.state == "shadow"
                active = await schedule_storage_activation(
                    conn,
                    measurement_basis="claim-requested",
                    activated_at=tomorrow,
                )
                assert active.state == "active"
                assert active.activated_at == tomorrow
                with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE storage_metering_source_activations "
                            "SET state='active',activated_at=$1 "
                            "WHERE measurement_basis='claim-requested' "
                            "AND collector_id='kubernetes-storage' "
                            "AND source_cluster='cluster-a'",
                            tomorrow - timedelta(days=1),
                        )
                with pytest.raises(StorageActivationNotReady, match="not active"):
                    await lock_storage_activation(
                        conn,
                        measurement_basis="claim-requested",
                        inventory_scope_id=claim_scope_id,
                        observed_started_at=now - timedelta(days=30),
                    )
    finally:
        await pool.close()
        admin = await asyncpg.connect(storage_pg_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            await admin.close()


@pytest.mark.asyncio
async def test_pv_only_0105_authority_materializes_reads_and_seals_on_postgres16(
    storage_pg_dsn: str,
    tmp_path: Path,
) -> None:
    """Prove the 0105 PV quantity/PVC attribution split through real SQL."""

    def migration_subset(name: str, through: str) -> Path:
        target = tmp_path / name
        target.mkdir()
        for migration in APP_MIGRATIONS.iterdir():
            prefix = migration.name.split("_", 1)[0]
            if (
                migration.is_file()
                and re.fullmatch(r"[0-9]{4}[a-z]?_.+\.sql", migration.name)
                and prefix <= through
            ):
                (target / migration.name).symlink_to(migration.resolve())
        return target

    through_0104 = migration_subset("pv-e2e-through-0104", "0104")
    through_0105 = migration_subset("pv-e2e-through-0105", "0105")
    dbname = f"storage_pv_e2e_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(storage_pg_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await admin.close()

    dsn = _swap_db(storage_pg_dsn, dbname)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    assert pool is not None
    try:
        await run_migrations(pool, through_0104)

        day = datetime.now(timezone.utc).date() - timedelta(days=3)
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        pv_scope_id, pvc_scope_id = uuid4(), uuid4()
        pv_epoch_id, pvc_epoch_id = uuid4(), uuid4()
        request_id, actor_id = uuid4(), uuid4()

        # 0102 permits only future activation through its public transition.
        # This isolated fixture disables the one-way row trigger briefly to
        # represent a boundary that elapsed before 0105 was deployed; 0105 then
        # performs its production legacy-authority backfill without bypasses.
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "ALTER TABLE infra_metering_control DISABLE TRIGGER "
                    "infra_metering_control_cutover_one_way"
                )
                await conn.execute(
                    "UPDATE infra_metering_control SET "
                    "leader_generation=7,cutover_state='active',"
                    "cutover_phase='active',cutover_at=$1,"
                    "cutover_request_id=$2,cutover_actor_id=$3,"
                    "cutover_reason='pv-only PostgreSQL integration fixture',"
                    "cutover_requested_at=$1,barrier_committed_at=$1,"
                    "legacy_drained_at=$1,activated_at=$1,cutover_error=NULL,"
                    "updated_at=statement_timestamp() WHERE singleton",
                    day_start,
                    request_id,
                    actor_id,
                )
                await conn.execute(
                    "ALTER TABLE infra_metering_control ENABLE TRIGGER "
                    "infra_metering_control_cutover_one_way"
                )
                await conn.execute(
                    "ALTER TABLE storage_metering_activation DISABLE TRIGGER "
                    "storage_metering_activation_one_way"
                )
                await conn.execute(
                    "UPDATE storage_metering_activation SET "
                    "state='active',activated_at=$1,"
                    "updated_at=statement_timestamp() "
                    "WHERE measurement_basis = ANY($2::text[])",
                    day_start,
                    ["claim-requested", "volume-provisioned"],
                )
                await conn.execute(
                    "ALTER TABLE storage_metering_activation ENABLE TRIGGER "
                    "storage_metering_activation_one_way"
                )
                await conn.executemany(
                    "INSERT INTO resource_inventory_scopes ("
                    "id,collector_id,source_cluster,api_resource,namespace"
                    ") VALUES ($1,'kubernetes-pods','pv-e2e',$2,$3)",
                    [
                        (
                            pv_scope_id,
                            "core/v1/persistentvolumes",
                            None,
                        ),
                        (
                            pvc_scope_id,
                            "core/v1/persistentvolumeclaims",
                            "srw",
                        ),
                    ],
                )
                await conn.executemany(
                    "INSERT INTO resource_inventory_scope_epochs ("
                    "id,scope_id,epoch_number,reliable_from,required_for_rollup,"
                    "required_from,coverage_mode,last_complete_at,"
                    "leader_generation,continuous_since,complete_through,"
                    "snapshot_health,continuity_health,item_health,"
                    "backend_health,publication_health"
                    ") VALUES ($1,$2,1,$3,TRUE,$3,'list-watch',$4,7,$3,$4,"
                    "'healthy','healthy','healthy','healthy','healthy')",
                    [
                        (pv_epoch_id, pv_scope_id, day_start, day_end),
                        (pvc_epoch_id, pvc_scope_id, day_start, day_end),
                    ],
                )

        await run_migrations(pool, through_0105)

        pv_interval_id, claim_interval_id = uuid4(), uuid4()
        pv_lifecycle_id, claim_lifecycle_id = uuid4(), uuid4()
        owner_id, user_id, project_id = uuid4(), uuid4(), uuid4()
        storage_asset_id = uuid4()
        claim_start = day_end + timedelta(hours=1)
        claim_end = claim_start + timedelta(hours=1)
        async with pool.acquire() as conn:
            async with conn.transaction():
                requirements = await conn.fetch(
                    "SELECT measurement_basis,inventory_scope_id,requirement_role "
                    "FROM storage_metering_source_requirements "
                    "WHERE collector_id='kubernetes-pods' "
                    "AND source_cluster='pv-e2e'"
                )
                assert {
                    (
                        row["measurement_basis"],
                        row["inventory_scope_id"],
                        row["requirement_role"],
                    )
                    for row in requirements
                } == {
                    ("claim-requested", pvc_scope_id, "quantity"),
                    ("volume-provisioned", pv_scope_id, "quantity"),
                    ("volume-provisioned", pvc_scope_id, "attribution"),
                }
                await conn.executemany(
                    "INSERT INTO resource_lifecycle_heads ("
                    "source_lifecycle_id,latest_revision_no"
                    ") VALUES ($1,1)",
                    [(pv_lifecycle_id,), (claim_lifecycle_id,)],
                )
                await conn.execute(
                    "INSERT INTO resource_intervals ("
                    "id,inventory_scope_id,source_cluster,source_kind,source_uid,"
                    "source_api_version,source_lifecycle_id,revision_no,"
                    "source_revision,namespace,name,category,resource,"
                    "measurement_basis,cost_domain,resource_class,"
                    "attribution_scope,owner_kind,owner_id,user_id,project_id,"
                    "attribution_source,attribution_quality,backing_resource_uid,"
                    "lifecycle_confidence,cpu_millicores,memory_bytes,storage_bytes,"
                    "capacity_source,capacity_quality,measurement_algorithm,"
                    "started_at,start_time_source,start_uncertainty_us,ended_at,"
                    "end_time_source,end_uncertainty_us,last_seen_at,"
                    "last_confirmed_at,materialized_through,end_reason,details"
                    ") VALUES ("
                    "$1,$2,'pv-e2e','volume',$3,'v1',$4,1,$5,NULL,'pv-e2e',"
                    "'storage','block_volume_local_path','volume-provisioned',"
                    "'physical-asset','persistent-volume','customer','job',$6,"
                    "$7,$8,'pv-claim-join','exact','pvc-attribution-uid',"
                    "'kubernetes-visible',NULL,NULL,$9,'pv-provisioned-capacity',"
                    "'exact','kubernetes-pv-capacity-v1',$10,'activation-boundary',"
                    "0,$11,'backend-close',0,$11,$11,$10,'fixture-close',$12::jsonb"
                    ")",
                    pv_interval_id,
                    pv_scope_id,
                    "d" * 64,
                    pv_lifecycle_id,
                    "a" * 64,
                    str(owner_id),
                    user_id,
                    project_id,
                    4 * 1024**3,
                    day_start,
                    day_start + timedelta(hours=1),
                    json.dumps(
                        {
                            "storage_asset_id": str(storage_asset_id),
                            "mapping_version": "pv-e2e-v1",
                            "mapping_fingerprint": "b" * 64,
                        }
                    ),
                )
                # This claim is in the same PVC scope used by the volume's
                # attribution requirement. It is otherwise publishable and its
                # resource class is enabled below, so only the exact source
                # policy can keep it out of the PV-only publication stream.
                await conn.execute(
                    "INSERT INTO resource_intervals ("
                    "id,inventory_scope_id,source_cluster,source_kind,source_uid,"
                    "source_api_version,source_lifecycle_id,revision_no,"
                    "source_revision,namespace,name,category,resource,"
                    "measurement_basis,cost_domain,resource_class,"
                    "attribution_scope,owner_kind,owner_id,user_id,project_id,"
                    "attribution_source,attribution_quality,lifecycle_confidence,"
                    "cpu_millicores,memory_bytes,storage_bytes,capacity_source,"
                    "capacity_quality,measurement_algorithm,started_at,"
                    "start_time_source,start_uncertainty_us,ended_at,"
                    "end_time_source,end_uncertainty_us,last_seen_at,"
                    "last_confirmed_at,materialized_through,end_reason,details"
                    ") VALUES ("
                    "$1,$2,'pv-e2e','pvc','pvc-attribution-uid','v1',$3,1,$4,"
                    "'srw','pvc-e2e','storage','workspace_pvc','claim-requested',"
                    "'workload-allocation','persistent-volume-claim','customer',"
                    "'job',$5,$6,$7,'pvc-label-db','exact','kubernetes-visible',"
                    "NULL,NULL,$8,'pvc-requested-storage','exact',"
                    "'kubernetes-pvc-request-v1',$9,'activation-boundary',0,$10,"
                    "'backend-close',0,$10,$10,$9,'fixture-close','{}'::jsonb"
                    ")",
                    claim_interval_id,
                    pvc_scope_id,
                    claim_lifecycle_id,
                    "c" * 64,
                    str(owner_id),
                    user_id,
                    project_id,
                    2 * 1024**3,
                    claim_start,
                    claim_end,
                )
                await conn.executemany(
                    "UPDATE resource_lifecycle_heads SET current_interval_id=$1 "
                    "WHERE source_lifecycle_id=$2",
                    [
                        (pv_interval_id, pv_lifecycle_id),
                        (claim_interval_id, claim_lifecycle_id),
                    ],
                )

                await conn.execute(
                    "ALTER TABLE resource_inventory_snapshots DISABLE TRIGGER "
                    "resource_inventory_snapshots_generation_fence"
                )
                for epoch_id, scope_id in (
                    (pv_epoch_id, pv_scope_id),
                    (pvc_epoch_id, pvc_scope_id),
                ):
                    snapshot_id = await conn.fetchval(
                        "INSERT INTO resource_inventory_snapshots ("
                        "scope_epoch_id,inventory_scope_id,collection_started_at,"
                        "collection_completed_at,received_at,complete,"
                        "leader_generation,item_count"
                        ") VALUES ($1,$2,$3,$3,$3,FALSE,7,0) RETURNING id",
                        epoch_id,
                        scope_id,
                        day_end - timedelta(seconds=1),
                    )
                    await conn.execute(
                        "UPDATE resource_inventory_snapshots SET "
                        "collection_completed_at=$2,received_at=$2,complete=TRUE,"
                        "item_digest=$3,manifest_state='sealed',sealed_at=$2 "
                        "WHERE id=$1",
                        snapshot_id,
                        day_end,
                        "0" * 64,
                    )
                await conn.execute(
                    "ALTER TABLE resource_inventory_snapshots ENABLE TRIGGER "
                    "resource_inventory_snapshots_generation_fence"
                )
                await conn.execute(
                    "CREATE OR REPLACE FUNCTION round_half_even_v2("
                    "value numeric, scale integer) RETURNS numeric "
                    "LANGUAGE sql IMMUTABLE STRICT AS 'SELECT round(value, scale)'"
                )
                await conn.execute(
                    "CREATE TABLE usage_events ("
                    "ts TIMESTAMPTZ NOT NULL,user_id UUID,project_id UUID,"
                    "ref_kind TEXT,ref_id UUID,category TEXT NOT NULL,"
                    "resource TEXT NOT NULL,quantity NUMERIC NOT NULL,"
                    "unit TEXT NOT NULL,rate_usd NUMERIC,cost_usd NUMERIC,"
                    "source TEXT NOT NULL,period_start TIMESTAMPTZ,"
                    "period_end TIMESTAMPTZ,measurement_basis TEXT,"
                    "cost_domain TEXT,resource_class TEXT,attribution_scope TEXT,"
                    "measurement_algorithm TEXT,source_capacity_value BIGINT,"
                    "source_capacity_unit TEXT,event_kind TEXT)"
                )

        policy = StoragePublicationPolicy(
            authorities=(
                StoragePublicationAuthority(
                    measurement_basis="volume-provisioned",
                    collector_id="kubernetes-pods",
                    source_cluster="pv-e2e",
                ),
            )
        )

        class LedgerStub:
            def __init__(self) -> None:
                self.published = []

            async def publish_frozen_events(self, events):
                self.published.extend(events)
                return StrictUsagePublishResult(
                    expected=len(events), inserted=len(events), verified=len(events)
                )

        ledger = LedgerStub()
        materializer = InfrastructureUsageMaterializer(
            pool,
            ledger,  # type: ignore[arg-type]
            publication_enabled=True,
            enabled_resources=("block_volume_local_path", "workspace_pvc"),
            storage_publication_policy=policy,
        )
        plans = await materializer.plan_batch(7)
        assert len(plans) == 1
        assert plans[0].source_interval_id == pv_interval_id
        assert {
            (item.event.payload["measurement_basis"], item.event.payload["unit"])
            for item in plans[0].events
        } == {
            ("volume-provisioned", "gib-hour"),
            ("volume-provisioned", "volume-hour"),
        }
        published = await materializer.publish_one(7)
        assert published is not None and published.cursor_advanced
        assert len(ledger.published) == 2
        assert await materializer.plan_batch(7) == ()

        async with pool.acquire() as conn:
            claim_cursor = await conn.fetchval(
                "SELECT materialized_through FROM resource_intervals WHERE id=$1",
                claim_interval_id,
            )
            claim_plan_count = await conn.fetchval(
                "SELECT count(*) FROM resource_publication_plans "
                "WHERE source_interval_id=$1",
                claim_interval_id,
            )
        assert claim_cursor == claim_start
        assert claim_plan_count == 0

        summary = await SourceAwareUsageReadModel(
            pool,
            pool,
            enabled_resources=("block_volume_local_path",),
            storage_publication_policy=policy,
        ).summary(
            from_ts=day_start,
            to_ts=day_end,
            visibility=UsageVisibility(),
            as_of=day_end,
        )
        assert summary.coverage.status == "complete"
        assert summary.coverage.required_sources_ok == 2
        assert summary.coverage.required_sources_total == 2
        assert summary.window.data_through == day_end
        assert {
            (row.measurement_basis, row.resource, row.unit, row.quantity)
            for row in summary.rows
        } == {
            (
                "volume-provisioned",
                "block_volume_local_path",
                "gib-hour",
                "4",
            ),
            (
                "volume-provisioned",
                "block_volume_local_path",
                "volume-hour",
                "1",
            ),
        }

        seal = await InfrastructureUsageDaySealer(
            pool,
            sealing_enabled=True,
            enabled_resources=("block_volume_local_path",),
            storage_publication_policy=policy,
        ).seal_day(day, 7)
        assert seal.disposition is DaySealDisposition.SEALED
        assert seal.coverage_status == "complete"
        assert seal.required_scopes == 2
    finally:
        await pool.close()
        admin = await asyncpg.connect(storage_pg_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            await admin.close()
