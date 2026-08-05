"""Static contracts for the Slice 0 infrastructure-metering migrations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import re
from uuid import uuid4

import asyncpg
import pytest


ROOT = Path(__file__).parents[1]
APP_MIGRATION = (
    ROOT
    / "orchestrator/database/migrations/app/0086_infrastructure_metering_foundations.sql"
)
AUDIT_EXPANSION = (
    ROOT
    / "orchestrator/database/migrations/audit/0003_infrastructure_usage_events_v2.sql"
)
AUDIT_VALIDATION = (
    ROOT
    / "orchestrator/database/migrations/audit/0004_validate_and_seed_infrastructure_usage_v2.sql"
)
AUDIT_PROJECT_INDEX = (
    ROOT / "orchestrator/database/migrations/audit/0005_usage_events_project_ts_idx.sql"
)


def _compact(sql: str) -> str:
    return " ".join(sql.split())


def _asyncpg_dsn(url: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def _swap_db(dsn: str, dbname: str) -> str:
    head, _, tail = dsn.rpartition("/")
    query = ""
    if "?" in tail:
        query = "?" + tail.split("?", 1)[1]
    return f"{head}/{dbname}{query}"


@pytest.fixture(scope="module")
def app_pg_dsn() -> str:
    testcontainers = pytest.importorskip("testcontainers.postgres")
    container = testcontainers.PostgresContainer("postgres:16")
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"no container runtime for app migration test: {exc}")
    try:
        yield _asyncpg_dsn(container.get_connection_url())
    finally:
        container.stop()


def test_app_migration_has_inventory_and_restrict_relationships() -> None:
    sql = APP_MIGRATION.read_text()

    for table in (
        "resource_inventory_scopes",
        "resource_inventory_scope_epochs",
        "resource_inventory_snapshots",
        "resource_inventory_snapshot_items",
        "resource_inventory_coverage_gaps",
        "infra_metering_control",
    ):
        assert f"CREATE TABLE {table}" in sql

    assert "resource_inventory_scopes_identity_uq" in sql
    assert "NULLS NOT DISTINCT" in sql
    assert "resource_inventory_scope_epochs_active_uq" in sql
    assert "WHERE retired_at IS NULL" in sql
    assert "resource_inventory_scopes_id_cluster_uq" in sql
    assert "valid_for_metering AND revision_hash IS NOT NULL" in _compact(sql)
    assert "FOREIGN KEY (last_complete_snapshot_id, id)" in sql
    assert "resource_inventory_snapshots_epoch_scope_fkey" in sql
    assert "manifest_state IN ('sealed', 'items-expired')" in sql
    assert "CREATE TRIGGER resource_inventory_snapshots_seal_only" in sql
    assert "BEFORE INSERT OR UPDATE ON resource_inventory_snapshots" in sql
    assert "CREATE TRIGGER resource_inventory_snapshot_items_staging_only" in sql
    assert "BEFORE INSERT OR UPDATE OR DELETE" in sql
    assert "INTERVAL '7 days'" in sql
    assert "ON DELETE CASCADE" not in sql
    assert sql.count("ON DELETE RESTRICT") >= 10


def test_interval_constraints_bind_identity_dimensions_and_time() -> None:
    sql = _compact(APP_MIGRATION.read_text())

    assert "resource_intervals_lifecycle_no_overlap EXCLUDE USING gist" in sql
    assert "source_lifecycle_id WITH =" in sql
    assert "resource_intervals_open_lifecycle_uq" in sql
    assert "resource_intervals_open_uq" in sql
    assert "resource_intervals_inventory_scope_cluster_fkey" in sql
    assert "resource_intervals_last_seen_snapshot_fkey" in sql
    assert "CREATE TRIGGER resource_intervals_scope_identity" in sql
    assert "CREATE TRIGGER resource_intervals_immutable_revision" in sql
    assert "FOREIGN KEY (current_interval_id, source_lifecycle_id)" in sql
    assert "AND (ended_at IS NULL OR ended_at >= started_at)" in sql
    assert "owner_kind IS NOT NULL AND owner_kind IN ('job', 'thread')" in sql
    assert "owner_id IS NOT NULL AND owner_id <> '' AND user_id IS NOT NULL" in sql
    assert "attribution_quality IN ('exact', 'derived')" in sql
    assert "resource_class = 'kubernetes-pod'" in sql
    assert "resource_class = 'virtual-machine'" in sql
    assert "resource_class = 'persistent-volume-claim'" in sql
    assert "resource_class = 'persistent-volume'" in sql
    assert "capacity_quality NOT IN ('unsupported', 'unknown', 'invalid')" in sql
    for confidence in (
        "backend-confirmed",
        "kubernetes-visible",
        "backend-unverified",
    ):
        assert confidence in sql


def test_app_migration_has_frozen_publication_and_rate_versions() -> None:
    sql = APP_MIGRATION.read_text()

    for table in (
        "usage_rates_v2",
        "usage_rate_card_versions_v2",
        "usage_rate_components_v2",
        "resource_lifecycle_heads",
        "resource_intervals",
        "resource_publication_plans",
        "resource_publication_plan_events",
    ):
        assert f"CREATE TABLE {table}" in sql

    assert "usage_rates_v2_no_overlap EXCLUDE USING gist" in sql
    assert "resource <> '*'" in sql
    assert "component_count" in sql
    assert "component_manifest_hash" in sql
    assert "usage_rate_card_versions_v2_manifest_complete" in sql
    assert "usage_rate_components_v2_manifest_complete" in sql
    assert "canonical_rate_version_id" in sql
    assert "UNIQUE (source, source_id, unit, ts)" in sql
    assert "resource_publication_plans_interval_revision_fkey" in sql
    assert "resource_publication_plan_events_plan_kind_time_fkey" in sql
    assert "event_set_hash" in sql
    assert "rate_selection_hash" in sql
    for trigger in (
        "resource_publication_plans_manifest_complete",
        "resource_publication_plan_events_manifest_complete",
    ):
        assert f"CREATE CONSTRAINT TRIGGER {trigger}" in sql
    for trigger in (
        "resource_publication_plans_frozen_intent",
        "resource_publication_plan_events_frozen",
        "usage_rates_v2_immutable",
        "usage_rate_card_versions_v2_immutable",
        "usage_rate_components_v2_immutable",
    ):
        assert f"CREATE TRIGGER {trigger}" in sql


def test_app_migration_has_v2_daily_and_bootstrap_gates() -> None:
    sql = _compact(APP_MIGRATION.read_text())

    for table in (
        "infra_usage_day_state",
        "usage_daily_v2",
        "usage_rollup_day_state",
        "usage_rollup_v2_bootstrap_state",
    ):
        assert f"CREATE TABLE {table}" in sql

    for measure in (
        "quantity NUMERIC(38, 18) NOT NULL",
        "cost_usd NUMERIC(38, 18)",
        "priced_quantity NUMERIC(38, 18) NOT NULL",
        "unpriced_quantity NUMERIC(38, 18) NOT NULL",
        "priced_events BIGINT NOT NULL",
        "unpriced_events BIGINT NOT NULL",
    ):
        assert measure in sql
    assert "usage_daily_v2_dims_uq" in sql
    assert "NULLS NOT DISTINCT" in sql
    assert "priced_events = 0 AND cost_usd IS NULL" in sql
    assert "priced_events > 0 AND cost_usd IS NOT NULL" in sql
    assert "VALUES (TRUE, 'pending')" in sql
    assert "VALUES ('usage_daily_v2', NULL)" in sql
    assert "reconciled_through_day = seeded_through_day" in sql
    assert "completed_at >= started_at" in sql
    assert "CREATE TRIGGER infra_usage_day_state_one_way_seal" in sql
    assert "OLD.state = 'sealed'" in sql
    assert "NEW.state NOT IN ('open', 'sealing')" in sql
    assert "NEW.state NOT IN ('sealing', 'sealed')" in sql


def test_audit_expansion_is_nullable_typed_and_validated_separately() -> None:
    expansion = AUDIT_EXPANSION.read_text()
    validation = AUDIT_VALIDATION.read_text()

    for column in (
        "period_start",
        "period_end",
        "measurement_basis",
        "cost_domain",
        "resource_class",
        "attribution_scope",
        "measurement_algorithm",
        "source_capacity_value",
        "source_capacity_unit",
        "source_cluster",
        "source_kind",
        "source_uid",
        "source_lifecycle_id",
        "source_interval_id",
        "event_kind",
        "corrects_source",
        "corrects_source_id",
        "corrects_unit",
        "corrects_ts",
        "correction_group_id",
        "correction_reason",
        "correction_actor_id",
        "discovered_at",
        "payload_hash",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in expansion

    constraints = (
        "usage_events_period_bounds_v2_check",
        "usage_events_infra_v2_contract_check",
        "usage_events_event_kind_v2_check",
    )
    for constraint in constraints:
        assert f"ADD CONSTRAINT {constraint}" in expansion
        assert f"VALIDATE CONSTRAINT {constraint}" in validation
    assert expansion.count(") NOT VALID;") == len(constraints)
    assert "source_capacity_value = trunc(source_capacity_value)" in expansion
    assert "CREATE OR REPLACE FUNCTION round_half_even_v2" in expansion
    assert "SET search_path = pg_catalog" in expansion
    assert "abs(quantity) < 100000000000000000000::NUMERIC" in expansion
    assert "quantity = trunc(quantity, 18)" in expansion
    assert "abs(rate_usd)" in expansion
    assert "rate_usd = trunc(rate_usd, 18)" in expansion
    assert "cost_usd = round_half_even_v2(" in expansion
    assert "unit = 'vcpu-hour'" in expansion
    assert "source_capacity_unit = 'millicore'" in expansion
    assert "unit = 'claim-hour'" in expansion
    assert "unit = 'volume-hour'" in expansion
    assert "source_capacity_unit = 'instance'" in expansion
    assert "source_capacity_value = 1" in expansion
    assert "corrects_unit = unit" in expansion
    assert "corrects_ts = period_start" in expansion
    for numeric_column in (
        "source_capacity_value",
        "quantity",
        "rate_usd",
        "cost_usd",
    ):
        assert f"{numeric_column} NOT IN (" in expansion


def test_audit_dirty_day_trigger_is_statement_batched_and_bootstrapped() -> None:
    expansion = _compact(AUDIT_EXPANSION.read_text())
    validation = _compact(AUDIT_VALIDATION.read_text())

    assert "CREATE TABLE IF NOT EXISTS usage_rollup_dirty_days" in expansion
    assert "CREATE TRIGGER usage_events_rollup_dirty_days" in expansion
    assert "REFERENCING NEW TABLE AS inserted_usage_events" in expansion
    assert "FOR EACH STATEMENT" in expansion
    assert "GROUP BY (inserted.ts AT TIME ZONE 'UTC')::DATE" in expansion
    assert "usage_rollup_dirty_days.revision + 1" in expansion
    assert "CREATE TRIGGER usage_events_append_only_v2" in expansion
    assert "BEFORE UPDATE OR DELETE ON usage_events" in expansion
    assert "FROM usage_events GROUP BY (ts AT TIME ZONE 'UTC')::DATE" in validation
    assert "ON CONFLICT (day) DO NOTHING" in validation


def test_audit_project_window_index_documents_partitioned_build() -> None:
    sql = _compact(AUDIT_PROJECT_INDEX.read_text())

    assert "CREATE INDEX IF NOT EXISTS usage_events_project_ts_idx" in sql
    assert "ON usage_events (project_id, ts)" in sql
    assert "PostgreSQL 16 cannot" in AUDIT_PROJECT_INDEX.read_text()


def test_migration_heads_are_unique_and_snapshots_are_not_the_contract() -> None:
    app_files = sorted(
        (ROOT / "orchestrator/database/migrations/app").glob(
            "[0-9][0-9][0-9][0-9]_*.sql"
        )
    )
    audit_files = sorted(
        (ROOT / "orchestrator/database/migrations/audit").glob(
            "[0-9][0-9][0-9][0-9]_*.sql"
        )
    )

    for files in (app_files, audit_files):
        prefixes = [path.name.split("_", 1)[0] for path in files]
        assert len(prefixes) == len(set(prefixes))
    assert app_files[-1].name == APP_MIGRATION.name
    assert audit_files[-1].name == AUDIT_PROJECT_INDEX.name
    assert "schema_current" not in APP_MIGRATION.read_text()
    assert "audit_schema_current" not in AUDIT_EXPANSION.read_text()


@pytest.mark.asyncio
async def test_app_composite_foreign_keys_fail_closed_on_postgres16(
    app_pg_dsn: str,
) -> None:
    dbname = f"metering_t_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(app_pg_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await admin.close()

    conn = await asyncpg.connect(_swap_db(app_pg_dsn, dbname))
    try:
        await conn.execute('CREATE EXTENSION "uuid-ossp"')
        await conn.execute("CREATE TABLE usage_rate_cards (id TEXT PRIMARY KEY)")
        await conn.execute(
            "CREATE TABLE rollup_state (name TEXT PRIMARY KEY, last_closed_day DATE)"
        )
        await conn.execute(APP_MIGRATION.read_text())

        scope_a, scope_b = uuid4(), uuid4()
        epoch_a, epoch_b = uuid4(), uuid4()
        snapshot_a, snapshot_b, snapshot_incomplete = uuid4(), uuid4(), uuid4()
        await conn.execute(
            "INSERT INTO resource_inventory_scopes "
            "(id, collector_id, source_cluster, api_resource) VALUES "
            "($1, 'kubernetes', 'cluster-a', 'core/v1/pods'), "
            "($2, 'kubernetes', 'cluster-b', 'core/v1/pods')",
            scope_a,
            scope_b,
        )
        await conn.execute(
            "INSERT INTO resource_inventory_scope_epochs "
            "(id, scope_id, epoch_number, coverage_mode) VALUES "
            "($1, $2, 1, 'list-watch'), ($3, $4, 1, 'list-watch')",
            epoch_a,
            scope_a,
            epoch_b,
            scope_b,
        )
        observed = datetime.now(timezone.utc).replace(microsecond=0)
        collection_started = observed - timedelta(seconds=1)
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "INSERT INTO resource_inventory_snapshots "
                "(scope_epoch_id, inventory_scope_id, collection_started_at, "
                "collection_completed_at, received_at, complete, "
                "leader_generation, item_count, item_digest, manifest_state, "
                "sealed_at) VALUES "
                "($1, $2, $3, $4, $4, TRUE, 1, 0, $5, 'sealed', now())",
                epoch_a,
                scope_a,
                collection_started,
                observed,
                "f" * 64,
            )
        metering_day = observed.date()
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "INSERT INTO infra_usage_day_state "
                "(day, state, coverage_status, coverage_revision, sealed_at) "
                "VALUES ($1, 'sealed', 'complete', 'revision-0', now())",
                metering_day,
            )
        await conn.execute(
            "INSERT INTO infra_usage_day_state (day) VALUES ($1)",
            metering_day,
        )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE infra_usage_day_state SET state='sealed', "
                "coverage_status='complete', coverage_revision='revision-1', "
                "sealed_at=now() WHERE day=$1",
                metering_day,
            )
        await conn.execute(
            "UPDATE infra_usage_day_state SET state='sealing' WHERE day=$1",
            metering_day,
        )
        await conn.execute(
            "UPDATE infra_usage_day_state SET state='sealed', "
            "coverage_status='complete', coverage_revision='revision-1', "
            "sealed_at=now(), updated_at=now() WHERE day=$1",
            metering_day,
        )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE infra_usage_day_state SET coverage_status='partial', "
                "coverage_revision='revision-2' WHERE day=$1",
                metering_day,
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "DELETE FROM infra_usage_day_state WHERE day=$1",
                metering_day,
            )
        await conn.execute(
            "INSERT INTO resource_inventory_snapshots "
            "(id, scope_epoch_id, inventory_scope_id, collection_started_at, "
            "collection_completed_at, received_at, complete, "
            "leader_generation, item_count) "
            "VALUES ($1, $2, $3, $4, $4, $4, FALSE, 1, 0), "
            "($5, $6, $7, $4, $4, $4, FALSE, 1, 0), "
            "($8, $2, $3, $4, $4, $4, FALSE, 1, 0)",
            snapshot_a,
            epoch_a,
            scope_a,
            collection_started,
            snapshot_b,
            epoch_b,
            scope_b,
            snapshot_incomplete,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO resource_inventory_snapshot_items "
                "(snapshot_id, source_kind, source_uid, normalized_item, "
                "valid_for_metering) VALUES ($1, 'pod', 'pod-a', '{}', TRUE)",
                snapshot_a,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE resource_inventory_snapshots "
                "SET collection_completed_at=$2, received_at=$2, "
                "complete=TRUE, item_count=1, item_digest=$3, "
                "manifest_state='sealed', sealed_at=statement_timestamp() "
                "WHERE id=$1",
                snapshot_incomplete,
                observed,
                "e" * 64,
            )
        await conn.execute(
            "UPDATE resource_inventory_snapshots "
            "SET collection_completed_at=$2, received_at=$2, "
            "complete=TRUE, item_digest=$3, manifest_state='sealed', "
            "sealed_at=statement_timestamp() WHERE id = ANY($1::uuid[])",
            [snapshot_a, snapshot_b],
            observed,
            "0" * 64,
        )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "INSERT INTO resource_inventory_snapshot_items "
                "(snapshot_id, source_kind, source_uid, revision_hash, "
                "normalized_item, valid_for_metering) VALUES "
                "($1, 'pod', 'late-pod', $2, '{}', TRUE)",
                snapshot_a,
                "1" * 64,
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE resource_inventory_snapshots SET item_digest=$2 WHERE id=$1",
                snapshot_a,
                "2" * 64,
            )
        await conn.execute(
            "UPDATE resource_inventory_scope_epochs "
            "SET last_complete_snapshot_id=$1 WHERE id=$2",
            snapshot_a,
            epoch_a,
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "UPDATE resource_inventory_scope_epochs "
                "SET last_complete_snapshot_id=$1 WHERE id=$2",
                snapshot_a,
                epoch_b,
            )

        lifecycle_a, lifecycle_b = uuid4(), uuid4()
        interval_a = uuid4()
        revision_a = "a" * 64
        await conn.execute(
            "INSERT INTO resource_lifecycle_heads (source_lifecycle_id) "
            "VALUES ($1), ($2)",
            lifecycle_a,
            lifecycle_b,
        )
        interval_sql = """
            INSERT INTO resource_intervals (
                id, inventory_scope_id, source_cluster, source_kind,
                source_uid, source_api_version, source_lifecycle_id,
                revision_no, source_revision, name, category, resource,
                measurement_basis, cost_domain, resource_class,
                attribution_scope, owner_kind, owner_id, user_id,
                attribution_source, attribution_quality,
                lifecycle_confidence, cpu_millicores, memory_bytes,
                capacity_source, capacity_quality, measurement_algorithm,
                started_at, start_time_source, start_uncertainty_us,
                last_seen_at, last_confirmed_at, last_seen_snapshot_id,
                materialized_through
            ) VALUES (
                $1, $2, $3, 'pod', $4, 'v1', $5, 1, $6,
                'workspace-pod', 'compute', 'workspace_pod',
                'scheduler-request', 'workload-allocation',
                'kubernetes-pod', 'customer', 'job', $7, $8,
                'db-owner', 'exact', 'kubernetes-visible', 1000,
                4294967296, 'admitted-request', 'exact',
                'pod-requests-test-v1', $9, 'first-observation', 0,
                $10, $10, $11, $9
            )
        """
        owner_id, user_id = str(uuid4()), uuid4()
        await conn.execute(
            interval_sql,
            interval_a,
            scope_a,
            "cluster-a",
            "pod-a",
            lifecycle_a,
            revision_a,
            owner_id,
            user_id,
            observed,
            observed + timedelta(hours=1),
            snapshot_a,
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                interval_sql,
                uuid4(),
                scope_a,
                "cluster-b",
                "pod-b",
                lifecycle_b,
                "b" * 64,
                str(uuid4()),
                uuid4(),
                observed,
                observed + timedelta(hours=1),
                snapshot_a,
            )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "UPDATE resource_intervals SET last_seen_snapshot_id=$1 WHERE id=$2",
                snapshot_b,
                interval_a,
            )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "UPDATE resource_intervals SET last_seen_snapshot_id=$1 WHERE id=$2",
                snapshot_incomplete,
                interval_a,
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE resource_intervals SET namespace='wrong-scope' WHERE id=$1",
                interval_a,
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE resource_intervals SET cpu_millicores=2000 WHERE id=$1",
                interval_a,
            )
        advanced_seen = observed + timedelta(hours=1, minutes=1)
        await conn.execute(
            "UPDATE resource_intervals SET last_seen_at=$2, "
            "last_confirmed_at=$2, updated_at=statement_timestamp() WHERE id=$1",
            interval_a,
            advanced_seen,
        )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE resource_intervals SET last_seen_at=$2 WHERE id=$1",
                interval_a,
                observed + timedelta(hours=1),
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "DELETE FROM resource_intervals WHERE id=$1",
                interval_a,
            )
        await conn.execute(
            "UPDATE resource_lifecycle_heads SET latest_revision_no=1, "
            "current_interval_id=$1 WHERE source_lifecycle_id=$2",
            interval_a,
            lifecycle_a,
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "UPDATE resource_lifecycle_heads SET current_interval_id=$1 "
                "WHERE source_lifecycle_id=$2",
                interval_a,
                lifecycle_b,
            )

        plan_id = uuid4()
        plan_sql = """
            INSERT INTO resource_publication_plans (
                id, source_interval_id, source_revision, plan_kind,
                plan_revision, advances_cursor, previous_materialized_through,
                period_start, period_end, expected_event_count,
                payload_schema_version, event_set_hash, rate_selection_hash,
                creator_generation
            ) VALUES (
                $1, $2, $3, 'usage', 0, TRUE, $4, $4, $5, 1, 1,
                $6, $7, 1
            )
        """
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                plan_sql,
                uuid4(),
                interval_a,
                "f" * 64,
                observed + timedelta(minutes=30),
                observed + timedelta(minutes=45),
                "e" * 64,
                "f" * 64,
            )

        event_sql = (
            "INSERT INTO resource_publication_plan_events "
            "(plan_id, ordinal, source, source_id, unit, ts, event_kind, "
            "row_hash, event_payload) VALUES "
            "($1, $2, 'infra-allocation-v2', $3, $4, $5, $6, $7, '{}')"
        )
        async with conn.transaction():
            await conn.execute(
                plan_sql,
                plan_id,
                interval_a,
                revision_a,
                observed,
                observed + timedelta(minutes=30),
                "c" * 64,
                "d" * 64,
            )
            await conn.execute(
                event_sql,
                plan_id,
                0,
                "event-0",
                "vcpu-hour",
                observed,
                "usage",
                "1" * 64,
            )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                event_sql,
                plan_id,
                1,
                "event-wrong-kind",
                "gib-hour",
                observed,
                "late-usage",
                "2" * 64,
            )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                event_sql,
                plan_id,
                1,
                "event-wrong-time",
                "gib-hour",
                observed + timedelta(minutes=1),
                "usage",
                "3" * 64,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                event_sql,
                plan_id,
                1,
                "event-late-append",
                "gib-hour",
                observed,
                "usage",
                "4" * 64,
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE resource_publication_plan_events SET row_hash=$2 "
                "WHERE plan_id=$1",
                plan_id,
                "5" * 64,
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE resource_publication_plans SET event_set_hash=$2 WHERE id=$1",
                plan_id,
                "6" * 64,
            )
        await conn.execute(
            "UPDATE resource_publication_plans SET state='published', "
            "attempt_count=1, last_attempt_at=statement_timestamp(), "
            "published_at=statement_timestamp() WHERE id=$1",
            plan_id,
        )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE resource_publication_plans SET attempt_count=2, "
                "last_attempt_at=statement_timestamp() WHERE id=$1",
                plan_id,
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                event_sql,
                plan_id,
                1,
                "event-after-publish",
                "gib-hour",
                observed,
                "usage",
                "7" * 64,
            )
        await conn.execute(
            "DELETE FROM resource_publication_plan_events WHERE plan_id=$1",
            plan_id,
        )
        await conn.execute(
            "DELETE FROM resource_publication_plans WHERE id=$1",
            plan_id,
        )

        rate_a, rate_b = uuid4(), uuid4()
        rate_start = observed - timedelta(days=1)
        rate_boundary = observed + timedelta(days=1)
        rate_insert_sql = """
            INSERT INTO usage_rates_v2 (
                id, cost_domain, measurement_basis, category,
                resource_class, resource, unit, effective_from,
                usd_per_unit, source, source_version
            ) VALUES (
                $1, 'workload-allocation', 'scheduler-request', 'compute',
                'kubernetes-pod', 'workspace_pod', 'vcpu-hour', $2,
                $3, 'operator', $4
            )
        """
        await conn.execute(rate_insert_sql, rate_a, rate_start, Decimal("0.1"), "v1")
        with pytest.raises(asyncpg.ExclusionViolationError):
            await conn.execute(
                rate_insert_sql, rate_b, rate_boundary, Decimal("0.2"), "v2"
            )
        async with conn.transaction():
            await conn.execute(
                "UPDATE usage_rates_v2 SET effective_to=$1 WHERE id=$2",
                rate_boundary,
                rate_a,
            )
            await conn.execute(
                rate_insert_sql, rate_b, rate_boundary, Decimal("0.2"), "v2"
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE usage_rates_v2 SET usd_per_unit=0.3 WHERE id=$1",
                rate_b,
            )

        await conn.execute("INSERT INTO usage_rate_cards (id) VALUES ('card-a')")
        version_sql = """
            INSERT INTO usage_rate_card_versions_v2 (
                id, card_id, provider, target_service, target_region,
                currency, pricing_basis, calculator, aggregation_scope,
                shape_change_policy, provider_effective_from, observed_at,
                source_version, source_checksum, component_count,
                component_manifest_hash
            ) VALUES (
                $1, 'card-a', 'stackit', 'ske', 'eu01', 'EUR',
                'historical-public-list', 'linear_v1', 'lifecycle',
                'continue', $2, $2, $3, $4, 1, $5
            )
        """
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(
                    version_sql,
                    uuid4(),
                    rate_start,
                    "incomplete",
                    "source-a",
                    "4" * 64,
                )

        version_id = uuid4()
        async with conn.transaction():
            await conn.execute(
                version_sql,
                version_id,
                rate_start,
                "complete",
                "source-b",
                "5" * 64,
            )
            await conn.execute(
                "INSERT INTO usage_rate_components_v2 "
                "(version_id, component, billing_unit, unit_size, unit_price) "
                "VALUES ($1, 'cpu', 'vcpu-hour', 1, 0.1)",
                version_id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO usage_rate_components_v2 "
                    "(version_id, component, billing_unit, unit_size, "
                    "unit_price) VALUES ($1, 'memory', 'gib-hour', 1, 0.1)",
                    version_id,
                )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE usage_rate_components_v2 SET unit_price=0.2 "
                "WHERE version_id=$1",
                version_id,
            )
    finally:
        await conn.close()
        admin = await asyncpg.connect(app_pg_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            await admin.close()
