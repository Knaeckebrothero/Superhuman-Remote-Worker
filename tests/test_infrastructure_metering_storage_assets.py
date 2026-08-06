"""Slice 2 physical-storage identity and app-DB foundation contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from orchestrator.services.infrastructure_metering.storage_assets import (
    StorageActivationNotReady,
    StorageAssetConflict,
    StorageAssetContractError,
    advance_storage_activation_to_shadow,
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
    volume_identity_key_fingerprint,
)
from orchestrator.services.infrastructure_metering.storage_mapping import (
    StorageResourceMappingRule,
    list_storage_resource_mapping_resources,
    register_storage_resource_mapping,
    resolve_storage_resource_mapping,
)


ROOT = Path(__file__).parents[1]
APP_MIGRATIONS = ROOT / "orchestrator/database/migrations/app"
STORAGE_MIGRATION = APP_MIGRATIONS / "0102_storage_asset_foundations.sql"


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
        tomorrow = (now + timedelta(days=1)).replace(
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
                with pytest.raises(StorageActivationNotReady, match="future"):
                    await lock_storage_activation(
                        conn,
                        measurement_basis="claim-requested",
                        observed_started_at=now - timedelta(days=30),
                    )
    finally:
        await pool.close()
        admin = await asyncpg.connect(storage_pg_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            await admin.close()
