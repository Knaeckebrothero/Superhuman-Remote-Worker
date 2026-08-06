"""Runtime schema probes for dark-launched metering code."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Mapping

import asyncpg

logger = logging.getLogger(__name__)


REQUIRED_APP_TABLES = frozenset(
    {
        "infra_metering_control",
        "infra_usage_day_state",
        "resource_intervals",
        "resource_inventory_coverage_gaps",
        "resource_inventory_scope_epochs",
        "resource_inventory_scopes",
        "resource_inventory_snapshot_items",
        "resource_inventory_snapshots",
        "resource_lifecycle_heads",
        "resource_publication_plan_events",
        "resource_publication_plans",
        "rollup_state",
        "usage_daily_v2",
        "usage_rate_card_versions_v2",
        "usage_rate_components_v2",
        "usage_rates_v2",
        "usage_rollup_day_state",
        "usage_rollup_v2_bootstrap_state",
    }
)

REQUIRED_AUDIT_TABLES = frozenset({"usage_events", "usage_rollup_dirty_days"})

REQUIRED_SLICE1_APP_TABLES = frozenset(
    {
        "resource_inventory_ingest_tickets",
        "resource_inventory_shadow_comparisons",
        "resource_inventory_transport_nonces",
        "resource_inventory_watch_events",
        "resource_inventory_watch_sessions",
    }
)

REQUIRED_APP_INDEXES = frozenset(
    {
        "resource_inventory_scope_epochs_active_uq",
        "resource_intervals_materializer_idx",
        "resource_intervals_open_lifecycle_uq",
        "resource_intervals_open_uq",
        "resource_publication_plans_pending_idx",
        "usage_daily_v2_dims_uq",
        "usage_rates_v2_lookup_idx",
    }
)

REQUIRED_SLICE1_APP_INDEXES = frozenset(
    {
        "resource_intervals_open_scope_identity_idx",
        "resource_inventory_ingest_tickets_expiry_idx",
        "resource_inventory_snapshots_sealed_retention_idx",
        "resource_inventory_snapshots_staging_retention_idx",
        "resource_inventory_shadow_comparisons_latest_idx",
        "resource_inventory_shadow_comparisons_unresolved_idx",
        "resource_inventory_transport_nonces_expiry_idx",
        "resource_inventory_watch_events_gap_idx",
        "resource_inventory_watch_events_scope_uid_idx",
        "resource_inventory_watch_sessions_live_idx",
        "resource_inventory_watch_sessions_retention_idx",
    }
)

REQUIRED_APP_TRIGGER_RELATIONS = {
    "infra_usage_day_state_one_way_seal": "infra_usage_day_state",
    "resource_inventory_snapshots_seal_only": "resource_inventory_snapshots",
    "resource_inventory_snapshot_items_staging_only": (
        "resource_inventory_snapshot_items"
    ),
    "resource_inventory_scope_epochs_complete_snapshot": (
        "resource_inventory_scope_epochs"
    ),
    "resource_intervals_scope_identity": "resource_intervals",
    "resource_intervals_immutable_revision": "resource_intervals",
    "resource_publication_plans_manifest_complete": "resource_publication_plans",
    "resource_publication_plan_events_manifest_complete": (
        "resource_publication_plan_events"
    ),
    "resource_publication_plans_frozen_intent": "resource_publication_plans",
    "resource_publication_plan_events_frozen": "resource_publication_plan_events",
    "usage_rate_card_versions_v2_manifest_complete": "usage_rate_card_versions_v2",
    "usage_rate_components_v2_manifest_complete": "usage_rate_components_v2",
    "usage_rates_v2_immutable": "usage_rates_v2",
    "usage_rate_card_versions_v2_immutable": "usage_rate_card_versions_v2",
    "usage_rate_components_v2_immutable": "usage_rate_components_v2",
}
REQUIRED_APP_TRIGGERS = frozenset(REQUIRED_APP_TRIGGER_RELATIONS)

REQUIRED_SLICE1_APP_TRIGGER_RELATIONS = {
    "infra_metering_control_monotonic_generation": "infra_metering_control",
    "resource_inventory_ingest_tickets_one_way": ("resource_inventory_ingest_tickets"),
    "resource_inventory_shadow_comparisons_immutable": (
        "resource_inventory_shadow_comparisons"
    ),
    "resource_inventory_snapshot_items_generation_fence": (
        "resource_inventory_snapshot_items"
    ),
    "resource_inventory_snapshots_generation_fence": ("resource_inventory_snapshots"),
    "resource_inventory_transport_nonces_immutable": (
        "resource_inventory_transport_nonces"
    ),
    "resource_inventory_watch_events_immutable": "resource_inventory_watch_events",
    "resource_inventory_watch_sessions_one_way": ("resource_inventory_watch_sessions"),
}
REQUIRED_SLICE1_APP_TRIGGERS = frozenset(REQUIRED_SLICE1_APP_TRIGGER_RELATIONS)

REQUIRED_AUDIT_TRIGGER_RELATIONS = {
    "usage_events_rollup_dirty_days": "usage_events",
    "usage_events_append_only_v2": "usage_events",
}

REQUIRED_AUDIT_INDEXES = frozenset(
    {
        "usage_events_dedupe_idx",
        "usage_events_project_ts_idx",
        "usage_rollup_dirty_days_pkey",
    }
)

REQUIRED_AUDIT_CONSTRAINTS = frozenset(
    {
        "usage_events_event_kind_v2_check",
        "usage_events_infra_v2_contract_check",
        "usage_events_period_bounds_v2_check",
    }
)

REQUIRED_AUDIT_COLUMNS = frozenset(
    {
        "attribution_scope",
        "correction_actor_id",
        "correction_group_id",
        "correction_reason",
        "corrects_source",
        "corrects_source_id",
        "corrects_ts",
        "corrects_unit",
        "cost_domain",
        "discovered_at",
        "event_kind",
        "measurement_algorithm",
        "measurement_basis",
        "payload_hash",
        "period_end",
        "period_start",
        "resource_class",
        "source_capacity_unit",
        "source_capacity_value",
        "source_cluster",
        "source_interval_id",
        "source_kind",
        "source_lifecycle_id",
        "source_uid",
    }
)


@dataclass(frozen=True)
class MeteringSchemaCapabilities:
    app_tables: frozenset[str] = frozenset()
    app_indexes: frozenset[str] = frozenset()
    app_triggers: frozenset[str] = frozenset()
    audit_tables: frozenset[str] = frozenset()
    audit_columns: frozenset[str] = frozenset()
    audit_constraints: frozenset[str] = frozenset()
    audit_indexes: frozenset[str] = frozenset()
    app_seed_rows_ready: bool = False
    half_even_function: bool = False
    dirty_day_trigger: bool = False
    append_only_trigger: bool = False
    target_partitions_ready: bool = False

    @property
    def missing_slice1_app_tables(self) -> frozenset[str]:
        return REQUIRED_SLICE1_APP_TABLES - self.app_tables

    @property
    def missing_slice1_app_indexes(self) -> frozenset[str]:
        return REQUIRED_SLICE1_APP_INDEXES - self.app_indexes

    @property
    def missing_slice1_app_triggers(self) -> frozenset[str]:
        return REQUIRED_SLICE1_APP_TRIGGERS - self.app_triggers

    @property
    def missing_app_tables(self) -> frozenset[str]:
        return REQUIRED_APP_TABLES - self.app_tables

    @property
    def missing_audit_tables(self) -> frozenset[str]:
        return REQUIRED_AUDIT_TABLES - self.audit_tables

    @property
    def missing_app_indexes(self) -> frozenset[str]:
        return REQUIRED_APP_INDEXES - self.app_indexes

    @property
    def missing_app_triggers(self) -> frozenset[str]:
        return REQUIRED_APP_TRIGGERS - self.app_triggers

    @property
    def missing_audit_columns(self) -> frozenset[str]:
        return REQUIRED_AUDIT_COLUMNS - self.audit_columns

    @property
    def missing_audit_constraints(self) -> frozenset[str]:
        return REQUIRED_AUDIT_CONSTRAINTS - self.audit_constraints

    @property
    def missing_audit_indexes(self) -> frozenset[str]:
        return REQUIRED_AUDIT_INDEXES - self.audit_indexes

    @property
    def v2_reads_ready(self) -> bool:
        return (
            not self.missing_audit_tables
            and not self.missing_audit_columns
            and not self.missing_audit_constraints
            and not self.missing_audit_indexes
            and self.half_even_function
            and self.dirty_day_trigger
            and self.append_only_trigger
        )

    @property
    def slice0_ready(self) -> bool:
        return (
            not self.missing_app_tables
            and not self.missing_app_indexes
            and not self.missing_app_triggers
            and self.app_seed_rows_ready
            and self.v2_reads_ready
            and self.target_partitions_ready
        )

    @property
    def slice1_inventory_ready(self) -> bool:
        """App-DB collector readiness, independent of the optional audit tier."""

        return (
            not self.missing_app_tables
            and not self.missing_app_indexes
            and not self.missing_app_triggers
            and self.app_seed_rows_ready
            and not self.missing_slice1_app_tables
            and not self.missing_slice1_app_indexes
            and not self.missing_slice1_app_triggers
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "slice0_ready": self.slice0_ready,
            "v2_reads_ready": self.v2_reads_ready,
            "missing_app_tables": sorted(self.missing_app_tables),
            "missing_app_indexes": sorted(self.missing_app_indexes),
            "missing_app_triggers": sorted(self.missing_app_triggers),
            "missing_audit_tables": sorted(self.missing_audit_tables),
            "missing_audit_columns": sorted(self.missing_audit_columns),
            "missing_audit_constraints": sorted(self.missing_audit_constraints),
            "missing_audit_indexes": sorted(self.missing_audit_indexes),
            "app_seed_rows_ready": self.app_seed_rows_ready,
            "half_even_function": self.half_even_function,
            "dirty_day_trigger": self.dirty_day_trigger,
            "append_only_trigger": self.append_only_trigger,
            "target_partitions_ready": self.target_partitions_ready,
            "slice1_inventory_ready": self.slice1_inventory_ready,
            "missing_slice1_app_tables": sorted(self.missing_slice1_app_tables),
            "missing_slice1_app_indexes": sorted(self.missing_slice1_app_indexes),
            "missing_slice1_app_triggers": sorted(self.missing_slice1_app_triggers),
        }


async def _table_names(pool: asyncpg.Pool | None, wanted: frozenset[str]) -> set[str]:
    if pool is None:
        return set()
    try:
        rows = await pool.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY($1::text[])",
            list(wanted),
        )
    except Exception:
        logger.warning("metering schema table probe failed", exc_info=True)
        return set()
    return {str(row["table_name"]) for row in rows}


async def _index_names(pool: asyncpg.Pool | None, wanted: frozenset[str]) -> set[str]:
    if pool is None:
        return set()
    try:
        rows = await pool.fetch(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' AND indexname = ANY($1::text[])",
            list(wanted),
        )
    except Exception:
        logger.warning("metering schema index probe failed", exc_info=True)
        return set()
    return {str(row["indexname"]) for row in rows}


async def _enabled_trigger_names(
    pool: asyncpg.Pool | None, wanted: Mapping[str, str]
) -> set[str]:
    if pool is None:
        return set()
    try:
        rows = await pool.fetch(
            "SELECT trigger.tgname, trigger.tgenabled::text AS enabled, "
            "relation.relname, namespace.nspname "
            "FROM pg_trigger AS trigger "
            "JOIN pg_class AS relation ON relation.oid = trigger.tgrelid "
            "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = 'public' "
            "AND trigger.tgname = ANY($1::text[])",
            list(wanted),
        )
    except Exception:
        logger.warning("metering schema trigger probe failed", exc_info=True)
        return set()
    return {
        str(row["tgname"])
        for row in rows
        if str(row["enabled"]) in {"O", "A"}
        and str(row["relname"]) == wanted.get(str(row["tgname"]))
    }


async def probe_schema_capabilities(
    app_pool: asyncpg.Pool | None,
    audit_pool: asyncpg.Pool | None,
) -> MeteringSchemaCapabilities:
    """Probe both databases; absence remains a normal, fail-closed state."""
    app_tables = await _table_names(
        app_pool, REQUIRED_APP_TABLES | REQUIRED_SLICE1_APP_TABLES
    )
    app_indexes = await _index_names(
        app_pool, REQUIRED_APP_INDEXES | REQUIRED_SLICE1_APP_INDEXES
    )
    app_triggers = await _enabled_trigger_names(
        app_pool,
        REQUIRED_APP_TRIGGER_RELATIONS | REQUIRED_SLICE1_APP_TRIGGER_RELATIONS,
    )
    audit_tables = await _table_names(audit_pool, REQUIRED_AUDIT_TABLES)
    audit_indexes = await _index_names(audit_pool, REQUIRED_AUDIT_INDEXES)
    audit_columns: set[str] = set()
    audit_constraints: set[str] = set()
    app_seed_rows_ready = False
    half_even_function = False
    dirty_trigger = False
    append_only_trigger = False
    target_partitions_ready = False
    if app_pool is not None and not (REQUIRED_APP_TABLES - app_tables):
        try:
            app_seed_rows_ready = bool(
                await app_pool.fetchval(
                    "SELECT "
                    "EXISTS (SELECT 1 FROM infra_metering_control WHERE singleton) "
                    "AND EXISTS (SELECT 1 FROM usage_rollup_v2_bootstrap_state "
                    "WHERE singleton) "
                    "AND EXISTS (SELECT 1 FROM rollup_state "
                    "WHERE name = 'usage_daily_v2')"
                )
            )
        except Exception:
            logger.warning("metering app seed-row probe failed", exc_info=True)
    if audit_pool is not None and "usage_events" in audit_tables:
        try:
            rows = await audit_pool.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'usage_events' "
                "AND column_name = ANY($1::text[])",
                list(REQUIRED_AUDIT_COLUMNS),
            )
            audit_columns = {str(row["column_name"]) for row in rows}
            rows = await audit_pool.fetch(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'usage_events'::regclass "
                "AND convalidated AND conname = ANY($1::text[])",
                list(REQUIRED_AUDIT_CONSTRAINTS),
            )
            audit_constraints = {str(row["conname"]) for row in rows}
            half_even_function = bool(
                await audit_pool.fetchval(
                    "SELECT to_regprocedure("
                    "'public.round_half_even_v2(numeric,integer)'"
                    ") IS NOT NULL"
                )
            )
            enabled_triggers = await _enabled_trigger_names(
                audit_pool,
                REQUIRED_AUDIT_TRIGGER_RELATIONS,
            )
            dirty_trigger = "usage_events_rollup_dirty_days" in enabled_triggers
            append_only_trigger = "usage_events_append_only_v2" in enabled_triggers
            target_partitions_ready = bool(
                await audit_pool.fetchval(
                    "WITH wanted AS ("
                    "SELECT 'usage_events_p' || to_char("
                    "date_trunc('month', now() AT TIME ZONE 'UTC') "
                    "+ make_interval(months => n), "
                    "'YYYY_MM') AS relname "
                    "FROM generate_series(0, 2) AS months(n)"
                    ") SELECT count(*) = 3 FROM wanted "
                    "JOIN pg_class child ON child.relname = wanted.relname "
                    "JOIN pg_namespace ns ON ns.oid = child.relnamespace "
                    "AND ns.nspname = 'public' "
                    "JOIN pg_inherits i ON i.inhrelid = child.oid "
                    "WHERE i.inhparent = 'usage_events'::regclass"
                )
            )
        except Exception:
            logger.warning("metering audit schema probe failed", exc_info=True)
    return MeteringSchemaCapabilities(
        app_tables=frozenset(app_tables),
        app_indexes=frozenset(app_indexes),
        app_triggers=frozenset(app_triggers),
        audit_tables=frozenset(audit_tables),
        audit_columns=frozenset(audit_columns),
        audit_constraints=frozenset(audit_constraints),
        audit_indexes=frozenset(audit_indexes),
        app_seed_rows_ready=app_seed_rows_ready,
        half_even_function=half_even_function,
        dirty_day_trigger=dirty_trigger,
        append_only_trigger=append_only_trigger,
        target_partitions_ready=target_partitions_ready,
    )
