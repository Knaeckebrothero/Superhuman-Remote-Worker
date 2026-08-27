"""Irreversible workspace -> typed infrastructure cutover contracts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import re
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

from orchestrator.services.infrastructure_metering.cutover import (
    CutoverBlocked,
    CutoverConflictError,
    CutoverResumeResult,
    CutoverStatus,
    CutoverPhase,
    FrozenLegacyWorkspaceEvent,
    InfrastructureWorkspaceCutover,
    LegacyWorkspaceFreezeRequest,
    LegacyWorkspaceLedgerConflict,
    LegacyWorkspacePublishResult,
    legacy_workspace_payload_hash,
)
from orchestrator.services.infrastructure_metering.inventory import (
    InventoryItem,
    InventoryScopeIdentity,
    InventoryStore,
    TransportNonceClaim,
    inventory_manifest_digest,
)


ROOT = Path(__file__).parents[1]
APP_MIGRATIONS = tuple(
    ROOT / "orchestrator/database/migrations/app" / name
    for name in (
        "0034_workspace_intervals.sql",
        "0086_infrastructure_metering_foundations.sql",
        "0087_inventory_ingestion_foundations.sql",
        "0088_inventory_ingestion_logical_size.sql",
        "0089_infrastructure_plan_period_idx.notx.sql",
        "0090_infrastructure_interval_overlap_idx.notx.sql",
        "0091_inventory_complete_received_idx.notx.sql",
        "0092_inventory_invalid_watch_received_idx.notx.sql",
        "0092z_infrastructure_day_sequence_backfill_prep.sql",
        "0093_infrastructure_workspace_cutover.sql",
        "0094_infrastructure_workspace_cutover_hardening.sql",
        "0095_infrastructure_epoch_lock_order.sql",
        "0096_infrastructure_legacy_barrier_hardening.sql",
        "0097_infrastructure_lifecycle_head_lock_order.sql",
        "0098_infrastructure_terminal_evidence.sql",
        "0099_infrastructure_terminal_evidence_equal_timestamp.sql",
        "0100_infrastructure_terminal_evidence_single_boundary.sql",
        "0101_usage_rates_v2_referenced_range_guard.sql",
    )
)
UTC = timezone.utc


def test_applied_cutover_migration_checksums_are_frozen() -> None:
    expected = {
        "0093_infrastructure_workspace_cutover.sql": (
            "06b698a432564f461fd4301ae84f816f22b0d4281e07a204b59e78104031912f"
        ),
        "0094_infrastructure_workspace_cutover_hardening.sql": (
            "7a1b228fc1991860e9af2b51d671857ce485b8d9aefbd7eaa6b8cd1bc61ecd39"
        ),
        "0098_infrastructure_terminal_evidence.sql": (
            "a7cf7e8888c2b8f3ea9d08fac1a7427d19b8efd30554ef226731d1e95524ff38"
        ),
        "0099_infrastructure_terminal_evidence_equal_timestamp.sql": (
            "a3aaa17a490155047304e4682c4f14eca46d7d42a3a44c40be3fd7d44c1acd10"
        ),
        "0100_infrastructure_terminal_evidence_single_boundary.sql": (
            "99a73fa991201f14e0c345509850a9c77c3f0533213548b80c57578e3179dd90"
        ),
        "0101_usage_rates_v2_referenced_range_guard.sql": (
            "8e8cf37541e874a9df6e7d2263b57f86f9553d78412b5b76df04d5a8e16eecf9"
        ),
    }

    for filename, checksum in expected.items():
        migration = ROOT / "orchestrator/database/migrations/app" / filename
        assert hashlib.sha256(migration.read_bytes()).hexdigest() == checksum


def _asyncpg_dsn(url: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def _swap_db(dsn: str, dbname: str) -> str:
    head, _, tail = dsn.rpartition("/")
    query = ""
    if "?" in tail:
        query = "?" + tail.split("?", 1)[1]
    return f"{head}/{dbname}{query}"


@pytest.fixture(scope="module")
def cutover_pg_dsn() -> str:
    testcontainers = pytest.importorskip("testcontainers.postgres")
    container = testcontainers.PostgresContainer("postgres:16")
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"no container runtime for cutover migration test: {exc}")
    try:
        yield _asyncpg_dsn(container.get_connection_url())
    finally:
        container.stop()


async def _create_database(
    base_dsn: str, prefix: str
) -> tuple[str, asyncpg.Connection]:
    dbname = f"{prefix}_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(base_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await admin.close()
    dsn = _swap_db(base_dsn, dbname)
    return dsn, await asyncpg.connect(dsn)


async def _apply_foundations(
    conn: asyncpg.Connection, *, preseed_sealed_day: bool = False
) -> None:
    await conn.execute('CREATE EXTENSION "uuid-ossp"')
    await conn.execute("CREATE TABLE usage_rate_cards (id TEXT PRIMARY KEY)")
    await conn.execute(
        "CREATE TABLE rollup_state (name TEXT PRIMARY KEY, last_closed_day DATE)"
    )
    await conn.execute(
        "CREATE TABLE jobs (id UUID PRIMARY KEY, user_id UUID, project_id UUID)"
    )
    await conn.execute(
        "CREATE TABLE threads (id UUID PRIMARY KEY, user_id UUID, project_id UUID)"
    )
    for migration in APP_MIGRATIONS[:-9]:
        await conn.execute(migration.read_text())
    if preseed_sealed_day:
        day = datetime.now(UTC).date() - timedelta(days=3)
        await conn.execute("INSERT INTO infra_usage_day_state (day) VALUES ($1)", day)
        await conn.execute(
            "UPDATE infra_usage_day_state SET state='sealing', "
            "updated_at=statement_timestamp() WHERE day=$1",
            day,
        )
        await conn.execute(
            "UPDATE infra_usage_day_state SET state='sealed', "
            "coverage_status='complete', coverage_revision='seal-v1:original', "
            "unknown_ranges='[]'::jsonb, sealed_at=statement_timestamp(), "
            "updated_at=statement_timestamp() WHERE day=$1",
            day,
        )
    for migration in APP_MIGRATIONS[-9:]:
        await conn.execute(migration.read_text())


async def _insert_terminal_evidence_interval(
    conn: asyncpg.Connection,
    *,
    scope_id: UUID,
    source_uid: str,
    boundary: datetime,
) -> UUID:
    lifecycle_id, interval_id = uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO resource_lifecycle_heads (source_lifecycle_id) VALUES ($1)",
        lifecycle_id,
    )
    await conn.execute(
        "INSERT INTO resource_intervals ("
        "id, inventory_scope_id, source_cluster, source_kind, source_uid, "
        "source_api_version, source_resource_version, source_lifecycle_id, "
        "revision_no, source_revision, namespace, name, category, resource, "
        "measurement_basis, cost_domain, resource_class, attribution_scope, "
        "owner_kind, owner_id, attribution_source, attribution_quality, "
        "lifecycle_confidence, cpu_millicores, memory_bytes, capacity_source, "
        "capacity_quality, measurement_algorithm, started_at, "
        "start_time_source, start_uncertainty_us, last_seen_at, "
        "last_confirmed_at, materialized_through) VALUES ("
        "$1, $2, 'terminal-test', 'pod', $3, 'v1', 'rv-terminal', $4, 1, $5, "
        "'workers', $3, 'compute', 'workspace_pod', 'scheduler-request', "
        "'workload-allocation', 'kubernetes-pod', 'shared-platform', "
        "'platform', 'platform', 'test-fixture', 'exact', "
        "'kubernetes-visible', 1000, 1073741824, 'scheduler-request', "
        "'exact', 'test-v1', $6, 'inventory-receipt', 0, $7, $7, $6)",
        interval_id,
        scope_id,
        source_uid,
        lifecycle_id,
        "a" * 64,
        boundary - timedelta(hours=1),
        boundary,
    )
    await conn.execute(
        "UPDATE resource_intervals SET ended_at=$2, "
        "end_time_source='app-db-received', end_uncertainty_us=0, "
        "end_reason='terminal-or-unscheduled', updated_at=statement_timestamp() "
        "WHERE id=$1",
        interval_id,
        boundary,
    )
    return interval_id


def _freeze_request() -> LegacyWorkspaceFreezeRequest:
    started = datetime(2026, 8, 5, 8, tzinfo=UTC)
    return LegacyWorkspaceFreezeRequest(
        workspace_interval_id=41,
        owner_kind="job",
        owner_id=uuid4(),
        tier="sandbox",
        cpu_millicores=8000,
        memory_bytes=16 * 1024**3,
        started_at=started,
        ended_at=started + timedelta(hours=1),
        user_id=uuid4(),
        project_id=uuid4(),
    )


def _transport(kind: str) -> TransportNonceClaim:
    return TransportNonceClaim(
        collector_id="kubernetes",
        request_nonce=uuid4(),
        request_kind=kind,
        request_digest="9" * 64,
    )


def _frozen_events(
    request: LegacyWorkspaceFreezeRequest,
) -> tuple[FrozenLegacyWorkspaceEvent, FrozenLegacyWorkspaceEvent]:
    return tuple(
        FrozenLegacyWorkspaceEvent(
            payload=payload,
            row_hash=legacy_workspace_payload_hash(payload),
        )
        for payload in request.draft_payloads()
    )  # type: ignore[return-value]


def test_legacy_freeze_contract_preserves_capacity_hours_and_identity() -> None:
    request = _freeze_request()
    payloads = request.draft_payloads()

    assert request.quantities == {
        "vcpu-hour": Decimal("8.0"),
        "gib-hour": Decimal("16.0"),
    }
    assert {payload["unit"] for payload in payloads} == {
        "vcpu-hour",
        "gib-hour",
    }
    assert {payload["quantity"] for payload in payloads} == {"8", "16"}
    assert all(payload["source_id"] == request.source_id for payload in payloads)
    assert all(payload["user_id"] == str(request.user_id) for payload in payloads)

    events = _frozen_events(request)
    InfrastructureWorkspaceCutover._validate_frozen_pair(request, events)
    assert len({event.row_hash for event in events}) == 2


def test_cutover_progress_and_inventory_readiness_are_explicit() -> None:
    status = CutoverStatus(
        state="preparing",
        phase=CutoverPhase.LEGACY_DRAINING,
        leader_generation=7,
        cutover_at=datetime.now(UTC),
        request_id=uuid4(),
        actor_id=uuid4(),
        reason="test",
        unplanned_intervals=1,
        planned=0,
        published=0,
        conflicts=0,
        open_legacy_intervals=0,
        cutover_error=None,
    )
    assert CutoverResumeResult(status, 1, 0).progressed
    assert not CutoverResumeResult(status, 0, 0).progressed
    with pytest.raises(CutoverBlocked, match="no active Pod"):
        InfrastructureWorkspaceCutover._validate_scope_epochs(
            (),
            7,
            datetime.now(UTC),
            source_cluster="cluster-a",
            namespace_allowlist=("workers",),
        )

    barrier = datetime.now(UTC)
    ready_epoch = {
        "scope_id": uuid4(),
        "source_cluster": "cluster-a",
        "namespace": "workers",
        "leader_generation": 7,
        "reliable_from": barrier - timedelta(minutes=2),
        "continuous_since": barrier - timedelta(minutes=2),
        "last_complete_snapshot_id": uuid4(),
        "complete_through": barrier - timedelta(seconds=1),
        "snapshot_health": "healthy",
        "continuity_health": "healthy",
        "item_health": "healthy",
        "backend_health": "healthy",
    }
    InfrastructureWorkspaceCutover._validate_scope_epochs(
        (ready_epoch,),
        7,
        barrier,
        source_cluster="cluster-a",
        namespace_allowlist=("workers",),
    )
    with pytest.raises(CutoverBlocked, match="continuously proven"):
        InfrastructureWorkspaceCutover._validate_scope_epochs(
            (
                {
                    **ready_epoch,
                    "complete_through": barrier - timedelta(minutes=16),
                },
            ),
            7,
            barrier,
            source_cluster="cluster-a",
            namespace_allowlist=("workers",),
            max_scope_age=timedelta(minutes=15),
        )
    with pytest.raises(CutoverBlocked, match="missing"):
        InfrastructureWorkspaceCutover._validate_scope_epochs(
            (ready_epoch,),
            7,
            barrier,
            source_cluster="cluster-a",
            namespace_allowlist=("workers", "agents"),
        )
    with pytest.raises(CutoverBlocked, match="not unique"):
        InfrastructureWorkspaceCutover._validate_scope_epochs(
            (ready_epoch, {**ready_epoch, "scope_id": uuid4()}),
            7,
            barrier,
            source_cluster="cluster-a",
            namespace_allowlist=("workers",),
        )


def test_cutover_migration_contains_irreversible_and_cutover_day_proofs() -> None:
    sql = "\n".join(migration.read_text() for migration in APP_MIGRATIONS[-9:])
    compact = " ".join(sql.split())

    assert "cutover_phase TEXT NOT NULL DEFAULT 'disabled'" in sql
    assert "CREATE TRIGGER infra_metering_control_cutover_one_way" in sql
    assert "CREATE TRIGGER workspace_intervals_cutover_open_barrier" in sql
    assert (
        "CREATE TRIGGER resource_intervals_cutover_serialization "
        "BEFORE INSERT OR UPDATE ON resource_intervals FOR EACH STATEMENT" in compact
    )
    assert "CREATE TABLE legacy_workspace_cutover_plans" in sql
    assert "CREATE TABLE legacy_workspace_cutover_plan_events" in sql
    assert sql.count("PRIMARY KEY (plan_id, ordinal)") == 1
    assert "initial cutover inventory boundary is immutable" in sql
    assert "NEW.required_from IS DISTINCT FROM durable_cutover" in compact
    assert "coverage_sequence BIGINT NOT NULL DEFAULT 0" in sql
    assert "ADD COLUMN infra_coverage_revision TEXT" in sql
    assert "reason_code = 'bounded-start-semantics'" in sql
    assert "observed_start_time_source = 'app-db-received'" in sql
    assert "resource_inventory_scope_epochs_boundary_insert_lock" in sql
    assert "resource_inventory_scope_epochs_boundary_update_lock" in sql
    assert "workspace_intervals_cutover_insert_lock" in sql
    assert "infra_metering_control_legacy_drain_immutable" in sql
    assert "resource_lifecycle_heads_cutover_serialization" in sql
    assert sql.count("FOR EACH STATEMENT") >= 3


@pytest.mark.asyncio
async def test_migration_backfills_seal_revision_and_enforces_one_way_barriers(
    cutover_pg_dsn: str,
) -> None:
    _, conn = await _create_database(cutover_pg_dsn, "metering_cutover_schema")
    try:
        await _apply_foundations(conn, preseed_sealed_day=True)
        sealed = await conn.fetchrow(
            "SELECT day, coverage_sequence, sealed_at FROM infra_usage_day_state"
        )
        assert sealed is not None
        assert sealed["coverage_sequence"] == 1

        unknown = '[{"start":"2026-08-05T01:00:00Z","end":"2026-08-05T02:00:00Z"}]'
        await conn.execute(
            "UPDATE infra_usage_day_state SET coverage_status='partial', "
            "coverage_revision='seal-v1:degraded', unknown_ranges=$2::jsonb, "
            "coverage_sequence=2, updated_at=statement_timestamp() WHERE day=$1",
            sealed["day"],
            unknown,
        )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE infra_usage_day_state SET state='sealing', "
                "coverage_sequence=3 WHERE day=$1",
                sealed["day"],
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE infra_usage_day_state SET coverage_revision='seal-v1:same', "
                "coverage_sequence=3, updated_at=statement_timestamp() WHERE day=$1",
                sealed["day"],
            )

        rollup_column = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='usage_rollup_day_state' "
            "AND column_name='infra_coverage_revision'"
        )
        assert rollup_column == "text"

        owner_id = uuid4()
        await conn.execute(
            "INSERT INTO workspace_intervals (owner_kind, owner_id, tier, "
            "cpu_millicores, mem_bytes) VALUES ('job', $1, 'sandbox', 1000, 1)",
            owner_id,
        )
        disabled_closed_at = await conn.fetchval("SELECT statement_timestamp()")
        await conn.execute(
            "INSERT INTO workspace_intervals (owner_kind, owner_id, tier, "
            "cpu_millicores, mem_bytes, started_at, ended_at) "
            "VALUES ('job', $1, 'sandbox', 1000, 1, $2, $2)",
            uuid4(),
            disabled_closed_at,
        )
        barrier = await conn.fetchval("SELECT statement_timestamp()")
        request_id, actor_id = uuid4(), uuid4()
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE infra_metering_control SET cutover_state='active', "
                "cutover_at=$1 WHERE singleton",
                barrier,
            )
        await conn.execute(
            "UPDATE infra_metering_control SET leader_generation=1, "
            "cutover_state='preparing', cutover_phase='legacy-draining', "
            "cutover_at=$1, cutover_request_id=$2, cutover_actor_id=$3, "
            "cutover_reason='migration test', cutover_requested_at=$1, "
            "barrier_committed_at=$1, updated_at=statement_timestamp() "
            "WHERE singleton",
            barrier,
            request_id,
            actor_id,
        )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "INSERT INTO workspace_intervals (owner_kind, owner_id, tier, "
                "cpu_millicores, mem_bytes) "
                "VALUES ('job', $1, 'sandbox', 1000, 1)",
                uuid4(),
            )
        await conn.execute(
            "UPDATE workspace_intervals SET ended_at=$2 WHERE owner_id=$1",
            owner_id,
            barrier,
        )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE workspace_intervals SET ended_at=NULL WHERE owner_id=$1",
                owner_id,
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "INSERT INTO workspace_intervals (owner_kind, owner_id, tier, "
                "cpu_millicores, mem_bytes, started_at, ended_at) "
                "VALUES ('job', $1, 'sandbox', 1000, 1, $2, $2)",
                uuid4(),
                barrier,
            )

        scope_id = uuid4()
        await conn.execute(
            "INSERT INTO resource_inventory_scopes "
            "(id, collector_id, source_cluster, api_resource, namespace) "
            "VALUES ($1, 'kubernetes', 'schema-test', 'core/v1/pods', 'workers')",
            scope_id,
        )
        await conn.execute(
            "INSERT INTO resource_inventory_scope_epochs ("
            "scope_id, epoch_number, reliable_from, required_for_rollup, "
            "required_from, coverage_mode) VALUES ($1, 1, $2, TRUE, $2, 'list')",
            scope_id,
            barrier,
        )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE resource_inventory_scope_epochs SET required_from=$2 "
                "WHERE scope_id=$1",
                scope_id,
                datetime.combine(barrier.date(), datetime.min.time(), tzinfo=UTC),
            )
        second_scope = uuid4()
        await conn.execute(
            "INSERT INTO resource_inventory_scopes "
            "(id, collector_id, source_cluster, api_resource, namespace) "
            "VALUES ($1, 'kubernetes', 'schema-test', 'core/v1/pods', 'other')",
            second_scope,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO resource_inventory_scope_epochs ("
                "scope_id, epoch_number, reliable_from, required_for_rollup, "
                "required_from, coverage_mode) "
                "VALUES ($1, 1, $2, TRUE, $2, 'list')",
                second_scope,
                barrier + timedelta(seconds=1),
            )
        drained_at = barrier + timedelta(microseconds=1)
        await conn.execute(
            "UPDATE infra_metering_control SET "
            "cutover_phase='ready-to-activate', legacy_drained_at=$1, "
            "updated_at=statement_timestamp() WHERE singleton",
            drained_at,
        )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE infra_metering_control SET cutover_state='active', "
                "cutover_phase='active', legacy_drained_at=$1, activated_at=$1, "
                "updated_at=statement_timestamp() WHERE singleton",
                drained_at + timedelta(microseconds=1),
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE infra_metering_control SET cutover_state='disabled', "
                "cutover_phase='disabled' WHERE singleton"
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_terminal_snapshot_boundary_is_linked_once_after_item_expiry(
    cutover_pg_dsn: str,
) -> None:
    dsn, setup = await _create_database(cutover_pg_dsn, "metering_terminal_link")
    pool: asyncpg.Pool | None = None
    try:
        await _apply_foundations(setup)
        scope_id, epoch_id = uuid4(), uuid4()
        scope = InventoryScopeIdentity(
            collector_id="kubernetes",
            source_cluster="terminal-test",
            api_resource="core/v1/pods",
            namespace="workers",
        )
        await setup.execute(
            "INSERT INTO resource_inventory_scopes "
            "(id, collector_id, source_cluster, api_resource, namespace) "
            "VALUES ($1, $2, $3, $4, $5)",
            scope_id,
            scope.collector_id,
            scope.source_cluster,
            scope.api_resource,
            scope.namespace,
        )
        await setup.execute(
            "INSERT INTO resource_inventory_scope_epochs "
            "(id, scope_id, epoch_number, coverage_mode) "
            "VALUES ($1, $2, 1, 'list-watch')",
            epoch_id,
            scope_id,
        )
        await setup.close()

        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        store = InventoryStore(
            pool,
            max_batch_bytes=100_000,
            max_snapshot_items=10,
            max_snapshot_bytes=1_000_000,
        )
        assert await store.activate_generation() == 1
        item = InventoryItem(
            source_kind="pod",
            source_uid="terminal-pod",
            revision_hash="b" * 64,
            normalized_item={
                "source_kind": "pod",
                "uid": "terminal-pod",
                "namespace": "workers",
            },
            valid_for_metering=True,
        )
        boundary = datetime.now(UTC) - timedelta(days=8)

        async def make_staging_snapshot(
            request_digest: str,
        ) -> tuple[str, UUID, UUID]:
            ticket = await store.issue_ingest_ticket(
                epoch_id,
                request_digest,
                scope=scope,
                transport=_transport("snapshot-ticket"),
            )
            snapshot_id = uuid4()
            await store.begin_snapshot(
                ticket.token,
                ticket.id,
                snapshot_id,
                boundary - timedelta(seconds=1),
                scope=scope,
                transport=_transport("snapshot-begin"),
            )
            await store.stage_items(
                ticket.token,
                ticket.id,
                snapshot_id,
                (item,),
                scope=scope,
                transport=_transport("snapshot-items"),
            )
            return ticket.token, ticket.id, snapshot_id

        _, first_ticket_id, first_snapshot_id = await make_staging_snapshot("1" * 64)
        _, second_ticket_id, second_snapshot_id = await make_staging_snapshot("2" * 64)
        async with pool.acquire() as conn:
            for ticket_id, snapshot_id in (
                (first_ticket_id, first_snapshot_id),
                (second_ticket_id, second_snapshot_id),
            ):
                await conn.execute(
                    "UPDATE resource_inventory_snapshots SET "
                    "collection_completed_at=$2, received_at=$2, complete=TRUE, "
                    "item_count=1, item_digest=$3, "
                    "reconciliation_summary='{}'::jsonb, "
                    "manifest_state='sealed', sealed_at=$2 WHERE id=$1",
                    snapshot_id,
                    boundary,
                    inventory_manifest_digest((item,)),
                )
                await conn.execute(
                    "UPDATE resource_inventory_ingest_tickets SET "
                    "consumed_at=statement_timestamp() WHERE id=$1",
                    ticket_id,
                )
            interval_id = await _insert_terminal_evidence_interval(
                conn,
                scope_id=scope_id,
                source_uid=item.source_uid,
                boundary=boundary,
            )
            await conn.execute(
                "UPDATE resource_intervals SET last_seen_snapshot_id=$2, "
                "updated_at=statement_timestamp() WHERE id=$1",
                interval_id,
                first_snapshot_id,
            )
            await conn.execute(
                "UPDATE resource_inventory_snapshots SET "
                "manifest_state='items-expired', "
                "items_expired_at=statement_timestamp() WHERE id=$1",
                first_snapshot_id,
            )
            await conn.execute(
                "DELETE FROM resource_inventory_snapshot_items WHERE snapshot_id=$1",
                first_snapshot_id,
            )

            with pytest.raises(
                asyncpg.ObjectNotInPrerequisiteStateError,
                match="already has immutable terminal snapshot evidence",
            ):
                await conn.execute(
                    "UPDATE resource_intervals SET last_seen_snapshot_id=$2, "
                    "updated_at=statement_timestamp() WHERE id=$1",
                    interval_id,
                    second_snapshot_id,
                )

            persisted = await conn.fetchrow(
                "SELECT interval.last_seen_at, interval.ended_at, "
                "interval.last_seen_snapshot_id, snapshot.manifest_state, "
                "(SELECT count(*) FROM resource_inventory_snapshot_items item "
                " WHERE item.snapshot_id=snapshot.id) AS retained_items "
                "FROM resource_intervals interval "
                "JOIN resource_inventory_snapshots snapshot "
                "ON snapshot.id=interval.last_seen_snapshot_id "
                "WHERE interval.id=$1",
                interval_id,
            )
            assert persisted["last_seen_at"] == persisted["ended_at"] == boundary
            assert persisted["last_seen_snapshot_id"] == first_snapshot_id
            assert persisted["manifest_state"] == "items-expired"
            assert persisted["retained_items"] == 0
            assert await conn.fetchval(
                "SELECT TRUE FROM pg_trigger trigger "
                "WHERE trigger.tgname="
                "'resource_intervals_snapshot_end_single_boundary_guard' "
                "AND NOT trigger.tgisinternal"
            )
    finally:
        if pool is not None:
            await pool.close()
        if not setup.is_closed():
            await setup.close()


@pytest.mark.asyncio
async def test_interstitial_backfill_prep_is_noop_after_cutover_migrations(
    cutover_pg_dsn: str,
) -> None:
    _, conn = await _create_database(cutover_pg_dsn, "metering_cutover_bridge")
    try:
        await _apply_foundations(conn)
        before = await conn.fetchval(
            "SELECT pg_get_functiondef('protect_infra_usage_day_state_mutation()'::regprocedure)"
        )
        bridge = next(
            migration
            for migration in APP_MIGRATIONS
            if migration.name == "0092z_infrastructure_day_sequence_backfill_prep.sql"
        )
        await conn.execute(bridge.read_text())
        after = await conn.fetchval(
            "SELECT pg_get_functiondef('protect_infra_usage_day_state_mutation()'::regprocedure)"
        )
        assert after == before
    finally:
        await conn.close()


class _StrictLegacyLedger:
    def __init__(self, *, conflict_on_freeze: set[int] | None = None) -> None:
        self.freeze_calls: list[int] = []
        self.publish_calls = 0
        self.conflict_on_freeze = conflict_on_freeze or set()

    async def freeze_legacy_workspace_events(
        self, request: LegacyWorkspaceFreezeRequest
    ) -> tuple[FrozenLegacyWorkspaceEvent, FrozenLegacyWorkspaceEvent]:
        self.freeze_calls.append(request.workspace_interval_id)
        if request.workspace_interval_id in self.conflict_on_freeze:
            raise LegacyWorkspaceLedgerConflict("immutable audit row differs")
        return _frozen_events(request)

    async def publish_frozen_legacy_workspace_events(
        self, events: tuple[FrozenLegacyWorkspaceEvent, ...]
    ) -> LegacyWorkspacePublishResult:
        self.publish_calls += 1
        return LegacyWorkspacePublishResult(
            expected=len(events), inserted=len(events), verified=len(events)
        )


async def _seed_cutover_candidate(
    conn: asyncpg.Connection,
    *,
    comparison_explained: bool = True,
) -> dict[str, UUID | int]:
    generation = 7
    now = await conn.fetchval("SELECT statement_timestamp()")
    assert isinstance(now, datetime)
    owner_id, user_id, project_id = uuid4(), uuid4(), uuid4()
    scope_id, epoch_id, snapshot_id, ticket_id = uuid4(), uuid4(), uuid4(), uuid4()
    lifecycle_id, interval_id = uuid4(), uuid4()
    await conn.execute(
        "UPDATE infra_metering_control SET leader_generation=$1, "
        "updated_at=statement_timestamp() WHERE singleton",
        generation,
    )
    await conn.execute(
        "UPDATE usage_rollup_v2_bootstrap_state SET status='complete', "
        "started_at=$1, seeded_through_day=$2, reconciled_through_day=$2, "
        "completed_at=$3, updated_at=statement_timestamp() WHERE singleton",
        now - timedelta(hours=1),
        now.date() - timedelta(days=1),
        now,
    )
    await conn.execute(
        "UPDATE rollup_state SET last_closed_day=$1 WHERE name='usage_daily_v2'",
        now.date() - timedelta(days=1),
    )
    await conn.execute(
        "INSERT INTO jobs (id, user_id, project_id) VALUES ($1, $2, $3)",
        owner_id,
        user_id,
        project_id,
    )
    await conn.execute(
        "INSERT INTO resource_inventory_scopes "
        "(id, collector_id, source_cluster, api_resource, namespace) "
        "VALUES ($1, 'kubernetes', 'cluster-a', 'core/v1/pods', 'workers')",
        scope_id,
    )
    await conn.execute(
        "INSERT INTO resource_inventory_scope_epochs ("
        "id, scope_id, epoch_number, reliable_from, coverage_mode, "
        "leader_generation, continuous_since, complete_through, "
        "snapshot_health, continuity_health, item_health, backend_health, "
        "publication_health) VALUES ("
        "$1, $2, 1, $3, 'list-watch', $4, $3, $5, "
        "'healthy', 'healthy', 'healthy', 'healthy', 'initializing')",
        epoch_id,
        scope_id,
        now - timedelta(minutes=5),
        generation,
        now - timedelta(seconds=1),
    )
    await conn.execute(
        "INSERT INTO resource_inventory_ingest_tickets ("
        "id, nonce_hash, scope_epoch_id, leader_generation, request_digest, "
        "max_snapshot_items, max_snapshot_bytes, expires_at) VALUES ("
        "$1, $2, $3, $4, $5, 10, 1048576, $6)",
        ticket_id,
        "1" * 64,
        epoch_id,
        generation,
        "2" * 64,
        now + timedelta(hours=1),
    )
    await conn.execute(
        "INSERT INTO resource_inventory_snapshots ("
        "id, scope_epoch_id, inventory_scope_id, collection_started_at, "
        "collection_completed_at, received_at, complete, leader_generation, "
        "item_count, ingest_ticket_id) VALUES ("
        "$1, $2, $3, $4, $4, $4, FALSE, $5, 0, $6)",
        snapshot_id,
        epoch_id,
        scope_id,
        now - timedelta(minutes=4),
        generation,
        ticket_id,
    )
    await conn.execute(
        "UPDATE resource_inventory_ingest_tickets SET "
        "bound_snapshot_id=$2, bound_at=statement_timestamp() WHERE id=$1",
        ticket_id,
        snapshot_id,
    )
    started_at = now - timedelta(minutes=3)
    confirmed_at = now - timedelta(seconds=30)
    workspace_interval_id = await conn.fetchval(
        "INSERT INTO workspace_intervals (owner_kind, owner_id, tier, "
        "cpu_millicores, mem_bytes, started_at) "
        "VALUES ('job', $1, 'sandbox', 2000, $2, $3) RETURNING id",
        owner_id,
        4 * 1024**3,
        started_at,
    )
    await conn.execute(
        "INSERT INTO resource_inventory_shadow_comparisons ("
        "snapshot_id, inventory_scope_id, source_uid, owner_kind, owner_id, "
        "owner_trusted, legacy_interval_id, legacy_cpu_millicores, "
        "legacy_memory_bytes, legacy_started_at, observed_cpu_millicores, "
        "observed_memory_bytes, observed_started_at, "
        "observed_start_time_source, observed_start_uncertainty_us, "
        "start_delta_us, status, reason_code, explained, comparison_at) VALUES ("
        "$1, $2, 'pod-a', 'job', $3, TRUE, $4, 2000, $5, $6, 2000, $5, "
        "$6, 'app-db-received', 30000000, 0, $7, $8, $9, $10)",
        snapshot_id,
        scope_id,
        owner_id,
        workspace_interval_id,
        4 * 1024**3,
        started_at,
        "matched" if comparison_explained else "capacity-mismatch",
        "capacity-and-start-match" if comparison_explained else "capacity-difference",
        comparison_explained,
        confirmed_at,
    )
    await conn.execute(
        "UPDATE resource_inventory_snapshots SET complete=TRUE, "
        "item_digest=$2, reconciliation_summary='{}'::jsonb, "
        "manifest_state='sealed', sealed_at=received_at "
        "WHERE id=$1",
        snapshot_id,
        "0" * 64,
    )
    await conn.execute(
        "UPDATE resource_inventory_ingest_tickets SET "
        "consumed_at=statement_timestamp() WHERE id=$1",
        ticket_id,
    )
    await conn.execute(
        "UPDATE resource_inventory_scope_epochs SET last_complete_at=$2, "
        "last_complete_snapshot_id=$3 WHERE id=$1",
        epoch_id,
        now - timedelta(minutes=4),
        snapshot_id,
    )
    await conn.execute(
        "INSERT INTO resource_lifecycle_heads "
        "(source_lifecycle_id, latest_revision_no) VALUES ($1, 1)",
        lifecycle_id,
    )
    await conn.execute(
        "INSERT INTO resource_intervals ("
        "id, inventory_scope_id, source_cluster, source_kind, source_uid, "
        "source_api_version, source_resource_version, source_lifecycle_id, "
        "revision_no, source_revision, namespace, name, category, resource, "
        "measurement_basis, cost_domain, resource_class, attribution_scope, "
        "owner_kind, owner_id, user_id, project_id, attribution_source, "
        "attribution_quality, lifecycle_confidence, cpu_millicores, "
        "memory_bytes, capacity_source, capacity_quality, measurement_algorithm, "
        "started_at, start_time_source, start_uncertainty_us, last_seen_at, "
        "last_confirmed_at, last_seen_snapshot_id, materialized_through, details) "
        "VALUES ($1, $2, 'cluster-a', 'pod', 'pod-a', 'v1', 'rv-a', $3, 1, $4, "
        "'workers', 'workspace-a', 'compute', 'workspace_pod', "
        "'scheduler-request', 'workload-allocation', 'kubernetes-pod', "
        "'customer', 'job', $5, $6, $7, 'job-label-db', 'exact', "
        "'kubernetes-visible', 2000, $8, 'pod-requests-v1', 'exact', "
        "'kubernetes-pod-requests-v1', $9, 'app-db-received', 30000000, "
        "$10, $10, $11, $9, '{}'::jsonb)",
        interval_id,
        scope_id,
        lifecycle_id,
        "a" * 64,
        str(owner_id),
        user_id,
        project_id,
        4 * 1024**3,
        started_at,
        confirmed_at,
        snapshot_id,
    )
    await conn.execute(
        "UPDATE resource_lifecycle_heads SET current_interval_id=$2 "
        "WHERE source_lifecycle_id=$1",
        lifecycle_id,
        interval_id,
    )
    return {
        "owner_id": owner_id,
        "scope_id": scope_id,
        "epoch_id": epoch_id,
        "interval_id": interval_id,
        "lifecycle_id": lifecycle_id,
        "workspace_interval_id": int(workspace_interval_id),
    }


async def _seed_closed_legacy_interval(conn: asyncpg.Connection) -> int:
    owner_id, user_id, project_id = uuid4(), uuid4(), uuid4()
    now = await conn.fetchval("SELECT statement_timestamp()")
    assert isinstance(now, datetime)
    await conn.execute(
        "INSERT INTO jobs (id, user_id, project_id) VALUES ($1, $2, $3)",
        owner_id,
        user_id,
        project_id,
    )
    interval_id = await conn.fetchval(
        "INSERT INTO workspace_intervals (owner_kind, owner_id, tier, "
        "cpu_millicores, mem_bytes, started_at, ended_at) "
        "VALUES ('job', $1, 'sandbox', 1000, $2, $3, $4) RETURNING id",
        owner_id,
        2 * 1024**3,
        now - timedelta(hours=2),
        now - timedelta(hours=1),
    )
    return int(interval_id)


@pytest.mark.asyncio
async def test_cutover_prepare_preflights_closed_legacy_audit_before_barrier(
    cutover_pg_dsn: str,
) -> None:
    dsn, setup = await _create_database(cutover_pg_dsn, "metering_cutover_preflight")
    pool: asyncpg.Pool | None = None
    try:
        await _apply_foundations(setup)
        await _seed_cutover_candidate(setup)
        closed_id = await _seed_closed_legacy_interval(setup)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        ledger = _StrictLegacyLedger()
        coordinator = InfrastructureWorkspaceCutover(
            pool,
            ledger,
            source_cluster="cluster-a",
            namespace_allowlist=("workers",),
        )

        prepared = await coordinator.prepare(
            7,
            actor_id=uuid4(),
            reason="verified closed legacy audit",
            idempotency_key=uuid4(),
        )

        assert prepared.state == "preparing"
        assert ledger.freeze_calls == [closed_id]
        assert ledger.publish_calls == 1
    finally:
        if pool is not None:
            await pool.close()
        await setup.close()


@pytest.mark.asyncio
async def test_cutover_prepare_keeps_barrier_disabled_on_legacy_audit_conflict(
    cutover_pg_dsn: str,
) -> None:
    dsn, setup = await _create_database(cutover_pg_dsn, "metering_cutover_conflict")
    pool: asyncpg.Pool | None = None
    try:
        await _apply_foundations(setup)
        ids = await _seed_cutover_candidate(setup)
        closed_id = await _seed_closed_legacy_interval(setup)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        ledger = _StrictLegacyLedger(conflict_on_freeze={closed_id})
        coordinator = InfrastructureWorkspaceCutover(
            pool,
            ledger,
            source_cluster="cluster-a",
            namespace_allowlist=("workers",),
        )

        with pytest.raises(CutoverBlocked, match="immutable conflict"):
            await coordinator.prepare(
                7,
                actor_id=uuid4(),
                reason="must fail before irreversible barrier",
                idempotency_key=uuid4(),
            )

        control = await setup.fetchrow(
            "SELECT cutover_state, cutover_at FROM infra_metering_control "
            "WHERE singleton"
        )
        assert control is not None
        assert control["cutover_state"] == "disabled"
        assert control["cutover_at"] is None
        assert await setup.fetchval(
            "SELECT ended_at IS NULL FROM workspace_intervals WHERE id=$1",
            ids["workspace_interval_id"],
        )
        assert (
            await setup.fetchval(
                "SELECT count(*) FROM resource_intervals WHERE source_lifecycle_id=$1",
                ids["lifecycle_id"],
            )
            == 1
        )
        assert (
            await setup.fetchval("SELECT count(*) FROM legacy_workspace_cutover_plans")
            == 0
        )
        assert ledger.freeze_calls == [closed_id]
        assert ledger.publish_calls == 0
    finally:
        if pool is not None:
            await pool.close()
        await setup.close()


@pytest.mark.asyncio
async def test_cutover_prepare_requires_zero_latest_unexplained_shadow_rows(
    cutover_pg_dsn: str,
) -> None:
    dsn, setup = await _create_database(cutover_pg_dsn, "metering_cutover_shadow")
    pool: asyncpg.Pool | None = None
    try:
        await _apply_foundations(setup)
        ids = await _seed_cutover_candidate(setup, comparison_explained=False)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        ledger = _StrictLegacyLedger()
        coordinator = InfrastructureWorkspaceCutover(
            pool,
            ledger,
            source_cluster="cluster-a",
            namespace_allowlist=("workers",),
        )

        with pytest.raises(CutoverBlocked, match="remain unexplained"):
            await coordinator.prepare(
                7,
                actor_id=uuid4(),
                reason="unexplained shadow must block",
                idempotency_key=uuid4(),
            )

        assert (
            await setup.fetchval(
                "SELECT cutover_state FROM infra_metering_control WHERE singleton"
            )
            == "disabled"
        )
        assert await setup.fetchval(
            "SELECT ended_at IS NULL FROM workspace_intervals WHERE id=$1",
            ids["workspace_interval_id"],
        )
        assert ledger.freeze_calls == []
        assert ledger.publish_calls == 0
    finally:
        if pool is not None:
            await pool.close()
        await setup.close()


@pytest.mark.asyncio
async def test_cutover_prepare_resume_is_atomic_fenced_and_crash_resumable(
    cutover_pg_dsn: str,
) -> None:
    dsn, setup = await _create_database(cutover_pg_dsn, "metering_cutover_flow")
    pool: asyncpg.Pool | None = None
    try:
        await _apply_foundations(setup)
        ids = await _seed_cutover_candidate(setup)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        ledger = _StrictLegacyLedger()
        coordinator = InfrastructureWorkspaceCutover(
            pool,
            ledger,
            source_cluster="cluster-a",
            namespace_allowlist=("workers",),
        )
        request_id, actor_id = uuid4(), uuid4()

        # Snapshot finalization names the rollup-boundary columns on every
        # complete snapshot. Its statement trigger must wait for control before
        # PostgreSQL locks the epoch row; cutover uses control -> epoch order.
        async with pool.acquire() as control_locker, pool.acquire() as epoch_updater:
            waiting_epoch_update: asyncio.Task[str] | None = None
            lock_tx = control_locker.transaction()
            await lock_tx.start()
            try:
                await control_locker.execute(
                    "SELECT singleton FROM infra_metering_control "
                    "WHERE singleton=TRUE FOR UPDATE"
                )
                waiting_epoch_update = asyncio.create_task(
                    epoch_updater.execute(
                        "UPDATE resource_inventory_scope_epochs SET "
                        "required_for_rollup=required_for_rollup, "
                        "required_from=required_from WHERE id=$1",
                        ids["epoch_id"],
                    )
                )
                await asyncio.sleep(0.05)
                assert not waiting_epoch_update.done()
                assert (
                    await asyncio.wait_for(
                        control_locker.fetchval(
                            "SELECT id FROM resource_inventory_scope_epochs "
                            "WHERE id=$1 FOR UPDATE",
                            ids["epoch_id"],
                        ),
                        timeout=1,
                    )
                    == ids["epoch_id"]
                )
            finally:
                await lock_tx.rollback()
                if waiting_epoch_update is not None:
                    await asyncio.wait_for(waiting_epoch_update, timeout=1)

        # The statement trigger must wait on the control row before PostgreSQL
        # locks a target interval. Otherwise the barrier's control->interval
        # order deadlocks with an ordinary interval update's interval->control.
        async with pool.acquire() as control_locker, pool.acquire() as interval_updater:
            waiting_update: asyncio.Task[str] | None = None
            lock_tx = control_locker.transaction()
            await lock_tx.start()
            try:
                await control_locker.execute(
                    "SELECT singleton FROM infra_metering_control "
                    "WHERE singleton=TRUE FOR UPDATE"
                )
                waiting_update = asyncio.create_task(
                    interval_updater.execute(
                        "UPDATE resource_intervals SET "
                        "updated_at=statement_timestamp() WHERE id=$1",
                        ids["interval_id"],
                    )
                )
                await asyncio.sleep(0.05)
                assert not waiting_update.done()
                assert (
                    await asyncio.wait_for(
                        control_locker.fetchval(
                            "SELECT id FROM resource_intervals WHERE id=$1 FOR UPDATE",
                            ids["interval_id"],
                        ),
                        timeout=1,
                    )
                    == ids["interval_id"]
                )
            finally:
                await lock_tx.rollback()
                if waiting_update is not None:
                    await asyncio.wait_for(waiting_update, timeout=1)

        # Lifecycle-head mutations participate in the same barrier transaction
        # as resource intervals. They must acquire control before a head row so
        # cutover can safely retain its control -> interval -> head order.
        async with pool.acquire() as control_locker, pool.acquire() as head_updater:
            waiting_head_update: asyncio.Task[str] | None = None
            lock_tx = control_locker.transaction()
            await lock_tx.start()
            try:
                await control_locker.execute(
                    "SELECT singleton FROM infra_metering_control "
                    "WHERE singleton=TRUE FOR UPDATE"
                )
                waiting_head_update = asyncio.create_task(
                    head_updater.execute(
                        "UPDATE resource_lifecycle_heads SET "
                        "updated_at=statement_timestamp() "
                        "WHERE source_lifecycle_id=$1",
                        ids["lifecycle_id"],
                    )
                )
                await asyncio.sleep(0.05)
                assert not waiting_head_update.done()
                assert (
                    await asyncio.wait_for(
                        control_locker.fetchval(
                            "SELECT source_lifecycle_id "
                            "FROM resource_lifecycle_heads "
                            "WHERE source_lifecycle_id=$1 FOR UPDATE",
                            ids["lifecycle_id"],
                        ),
                        timeout=1,
                    )
                    == ids["lifecycle_id"]
                )
            finally:
                await lock_tx.rollback()
                if waiting_head_update is not None:
                    await asyncio.wait_for(waiting_head_update, timeout=1)

        prepared = await coordinator.prepare(
            7,
            actor_id=actor_id,
            reason="promote tested shadow inventory",
            idempotency_key=request_id,
        )
        assert prepared.state == "preparing"
        assert prepared.phase is CutoverPhase.LEGACY_DRAINING
        assert prepared.request_id == request_id
        assert prepared.cutover_at is not None

        legacy = await setup.fetchrow(
            "SELECT ended_at, materialized_at FROM workspace_intervals "
            "WHERE owner_id=$1",
            ids["owner_id"],
        )
        intervals = await setup.fetch(
            "SELECT id, started_at, ended_at, materialized_through, "
            "start_time_source, end_time_source FROM resource_intervals "
            "WHERE source_lifecycle_id=$1 ORDER BY revision_no",
            ids["lifecycle_id"],
        )
        epoch = await setup.fetchrow(
            "SELECT required_for_rollup, required_from "
            "FROM resource_inventory_scope_epochs WHERE id=$1",
            ids["epoch_id"],
        )
        assert legacy is not None and legacy["ended_at"] == prepared.cutover_at
        assert len(intervals) == 2
        assert intervals[0]["ended_at"] == prepared.cutover_at
        assert intervals[0]["end_time_source"] == "cutover-barrier"
        assert intervals[1]["started_at"] == prepared.cutover_at
        assert intervals[1]["materialized_through"] == prepared.cutover_at
        assert intervals[1]["start_time_source"] == "cutover-barrier"
        assert epoch is not None and epoch["required_for_rollup"] is True
        assert epoch["required_from"] == prepared.cutover_at

        assert (
            await coordinator.prepare(
                7,
                actor_id=actor_id,
                reason="promote tested shadow inventory",
                idempotency_key=request_id,
            )
        ).request_id == request_id
        with pytest.raises(CutoverConflictError, match="actor or reason"):
            await coordinator.prepare(
                7,
                actor_id=actor_id,
                reason="changed reason",
                idempotency_key=request_id,
            )
        with pytest.raises(CutoverConflictError, match="actor or reason"):
            await coordinator.prepare(
                7,
                actor_id=uuid4(),
                reason="promote tested shadow inventory",
                idempotency_key=request_id,
            )
        with pytest.raises(CutoverConflictError):
            await coordinator.prepare(
                7,
                actor_id=actor_id,
                reason="different request",
                idempotency_key=uuid4(),
            )

        first = await coordinator.resume(7, idempotency_key=request_id)
        assert first.progressed
        assert first.plans_frozen == 1
        assert first.plans_published == 1
        assert first.status.phase is CutoverPhase.READY_TO_ACTIVATE

        # A new process instance resumes from the durable request and plans.
        restarted = InfrastructureWorkspaceCutover(
            pool,
            ledger,
            source_cluster="cluster-a",
            namespace_allowlist=("workers",),
        )
        second = await restarted.resume(7, idempotency_key=request_id)
        assert not second.progressed
        assert second.status.active
        assert ledger.freeze_calls == [ids["workspace_interval_id"]]
        assert ledger.publish_calls == 1
        materialized = await setup.fetchval(
            "SELECT materialized_at IS NOT NULL FROM workspace_intervals "
            "WHERE owner_id=$1",
            ids["owner_id"],
        )
        assert materialized is True
        with pytest.raises(CutoverConflictError):
            await restarted.resume(7, idempotency_key=uuid4())
    finally:
        if pool is not None:
            await pool.close()
        await setup.close()
