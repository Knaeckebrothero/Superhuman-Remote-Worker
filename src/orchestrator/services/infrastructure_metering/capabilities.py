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

# The collector/shadow schema can be deployed and exercised independently of
# the irreversible workspace cutover.  Keep the final Slice 1 runtime contract
# separate so a rolling deployment on 0087--0092 can never start publication
# merely because inventory collection is healthy.
REQUIRED_SLICE1_RUNTIME_APP_TABLES = frozenset(
    {
        "legacy_workspace_cutover_plan_events",
        "legacy_workspace_cutover_plans",
    }
)

REQUIRED_SLICE1_RUNTIME_APP_COLUMNS = frozenset(
    {
        "infra_metering_control.barrier_committed_at",
        "infra_metering_control.cutover_actor_id",
        "infra_metering_control.cutover_error",
        "infra_metering_control.cutover_phase",
        "infra_metering_control.cutover_reason",
        "infra_metering_control.cutover_request_id",
        "infra_metering_control.cutover_requested_at",
        "infra_metering_control.legacy_drained_at",
        "infra_metering_control.activated_at",
        "infra_usage_day_state.coverage_sequence",
        "usage_rollup_day_state.infra_coverage_revision",
    }
)

REQUIRED_APP_INDEX_RELATIONS = {
    "resource_inventory_scope_epochs_active_uq": "resource_inventory_scope_epochs",
    "resource_intervals_materializer_idx": "resource_intervals",
    "resource_intervals_overlap_idx": "resource_intervals",
    "resource_intervals_open_lifecycle_uq": "resource_intervals",
    "resource_intervals_open_uq": "resource_intervals",
    "resource_publication_plans_pending_idx": "resource_publication_plans",
    "resource_publication_plans_period_idx": "resource_publication_plans",
    "usage_daily_v2_dims_uq": "usage_daily_v2",
    "usage_rates_v2_lookup_idx": "usage_rates_v2",
}
REQUIRED_APP_INDEXES = frozenset(REQUIRED_APP_INDEX_RELATIONS)

REQUIRED_SLICE1_APP_INDEX_RELATIONS = {
    "resource_intervals_open_scope_identity_idx": "resource_intervals",
    "resource_inventory_ingest_tickets_expiry_idx": (
        "resource_inventory_ingest_tickets"
    ),
    "resource_inventory_snapshots_complete_received_idx": (
        "resource_inventory_snapshots"
    ),
    "resource_inventory_snapshots_sealed_retention_idx": (
        "resource_inventory_snapshots"
    ),
    "resource_inventory_snapshots_staging_retention_idx": (
        "resource_inventory_snapshots"
    ),
    "resource_inventory_shadow_comparisons_latest_idx": (
        "resource_inventory_shadow_comparisons"
    ),
    "resource_inventory_shadow_comparisons_unresolved_idx": (
        "resource_inventory_shadow_comparisons"
    ),
    "resource_inventory_transport_nonces_expiry_idx": (
        "resource_inventory_transport_nonces"
    ),
    "resource_inventory_watch_events_gap_idx": "resource_inventory_watch_events",
    "resource_inventory_watch_events_invalid_received_idx": (
        "resource_inventory_watch_events"
    ),
    "resource_inventory_watch_events_scope_uid_idx": (
        "resource_inventory_watch_events"
    ),
    "resource_inventory_watch_sessions_live_idx": ("resource_inventory_watch_sessions"),
    "resource_inventory_watch_sessions_retention_idx": (
        "resource_inventory_watch_sessions"
    ),
}
REQUIRED_SLICE1_APP_INDEXES = frozenset(REQUIRED_SLICE1_APP_INDEX_RELATIONS)

REQUIRED_SLICE1_RUNTIME_APP_INDEX_RELATIONS = {
    "legacy_workspace_cutover_plans_pending_idx": ("legacy_workspace_cutover_plans"),
    "resource_publication_plan_events_rate_reference_idx": (
        "resource_publication_plan_events"
    ),
}
REQUIRED_SLICE1_RUNTIME_APP_INDEXES = frozenset(
    REQUIRED_SLICE1_RUNTIME_APP_INDEX_RELATIONS
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

REQUIRED_SLICE1_RUNTIME_APP_TRIGGER_RELATIONS = {
    "infra_metering_control_cutover_one_way": "infra_metering_control",
    "infra_metering_control_legacy_drain_immutable": "infra_metering_control",
    "resource_inventory_scope_epochs_boundary_insert_lock": (
        "resource_inventory_scope_epochs"
    ),
    "resource_inventory_scope_epochs_boundary_update_lock": (
        "resource_inventory_scope_epochs"
    ),
    "resource_inventory_scope_epochs_boundary_insert": (
        "resource_inventory_scope_epochs"
    ),
    "resource_inventory_scope_epochs_boundary_update": (
        "resource_inventory_scope_epochs"
    ),
    "workspace_intervals_cutover_open_barrier": "workspace_intervals",
    "workspace_intervals_cutover_insert_lock": "workspace_intervals",
    "resource_intervals_cutover_serialization": "resource_intervals",
    "resource_lifecycle_heads_cutover_serialization": "resource_lifecycle_heads",
    "resource_intervals_snapshot_end_single_boundary_guard": "resource_intervals",
    "resource_inventory_watch_events_terminal_evidence_guard": (
        "resource_inventory_watch_events"
    ),
    "usage_rates_v2_referenced_range_guard": "usage_rates_v2",
    "legacy_workspace_cutover_plans_frozen": "legacy_workspace_cutover_plans",
    "legacy_workspace_cutover_plan_events_frozen": (
        "legacy_workspace_cutover_plan_events"
    ),
    "legacy_workspace_cutover_plan_manifest_complete": (
        "legacy_workspace_cutover_plans"
    ),
    "legacy_workspace_cutover_plan_event_manifest_complete": (
        "legacy_workspace_cutover_plan_events"
    ),
}
REQUIRED_SLICE1_RUNTIME_APP_TRIGGERS = frozenset(
    REQUIRED_SLICE1_RUNTIME_APP_TRIGGER_RELATIONS
)

REQUIRED_SLICE1_RUNTIME_APP_CONSTRAINT_RELATIONS = {
    "infra_metering_control_cutover_phase_check": "infra_metering_control",
    "infra_metering_control_cutover_error_check": "infra_metering_control",
    "infra_metering_control_cutover_request_uq": "infra_metering_control",
    "legacy_workspace_cutover_plans_shape_check": ("legacy_workspace_cutover_plans"),
    "legacy_workspace_cutover_plan_events_shape_check": (
        "legacy_workspace_cutover_plan_events"
    ),
    "infra_usage_day_state_coverage_sequence_check": "infra_usage_day_state",
    "usage_rollup_day_state_infra_revision_check": "usage_rollup_day_state",
}
REQUIRED_SLICE1_RUNTIME_APP_CONSTRAINTS = frozenset(
    REQUIRED_SLICE1_RUNTIME_APP_CONSTRAINT_RELATIONS
)

# Slice 2 storage inventory is independently dark-launched. Claim demand needs
# only the forward activation/shadow contract; physical-volume inventory also
# requires opaque asset identity and retained-backend evidence. Keep these sets
# separate so granting cluster-scoped PV RBAC can never be inferred from PVC
# schema readiness.
REQUIRED_SLICE2_CLAIM_APP_TABLES = frozenset(
    {
        "storage_metering_activation",
        "storage_shadow_observations",
    }
)

REQUIRED_SLICE2_VOLUME_APP_TABLES = frozenset(
    {
        "infrastructure_storage_resource_mappings",
        "storage_asset_coverage_gaps",
        "storage_backend_assertions",
        "storage_identity_key_state",
        "storage_volume_assets",
        "storage_volume_incarnations",
    }
)

REQUIRED_SLICE2_CLAIM_APP_INDEX_RELATIONS = {
    "storage_shadow_observations_snapshot_identity_uq": ("storage_shadow_observations"),
    "storage_shadow_observations_latest_idx": "storage_shadow_observations",
}
REQUIRED_SLICE2_CLAIM_APP_INDEXES = frozenset(REQUIRED_SLICE2_CLAIM_APP_INDEX_RELATIONS)

REQUIRED_SLICE2_VOLUME_APP_INDEX_RELATIONS = {
    "infrastructure_storage_resource_mappings_pkey": (
        "infrastructure_storage_resource_mappings"
    ),
    "infrastructure_storage_resource_mappings_selector_uq": (
        "infrastructure_storage_resource_mappings"
    ),
    "infrastructure_storage_resource_mappings_resource_idx": (
        "infrastructure_storage_resource_mappings"
    ),
    "storage_volume_assets_identity_uq": "storage_volume_assets",
    "storage_volume_assets_state_idx": "storage_volume_assets",
    "storage_volume_incarnations_pv_uid_uq": "storage_volume_incarnations",
    "storage_volume_incarnations_active_asset_uq": "storage_volume_incarnations",
    "storage_asset_coverage_gaps_open_uq": "storage_asset_coverage_gaps",
    "storage_asset_coverage_gaps_range_idx": "storage_asset_coverage_gaps",
    "storage_backend_assertions_idempotency_uq": "storage_backend_assertions",
    "storage_backend_assertions_asset_uq": "storage_backend_assertions",
}
REQUIRED_SLICE2_VOLUME_APP_INDEXES = frozenset(
    REQUIRED_SLICE2_VOLUME_APP_INDEX_RELATIONS
)

REQUIRED_SLICE2_CLAIM_APP_TRIGGER_RELATIONS = {
    "storage_metering_activation_one_way": "storage_metering_activation",
    "resource_intervals_storage_activation_guard": "resource_intervals",
    "storage_shadow_observations_immutable": "storage_shadow_observations",
}
REQUIRED_SLICE2_CLAIM_APP_TRIGGERS = frozenset(
    REQUIRED_SLICE2_CLAIM_APP_TRIGGER_RELATIONS
)

REQUIRED_SLICE2_VOLUME_APP_TRIGGER_RELATIONS = {
    "infrastructure_storage_resource_mappings_append_only": (
        "infrastructure_storage_resource_mappings"
    ),
    "storage_identity_key_state_immutable": "storage_identity_key_state",
    "storage_volume_assets_lifecycle_guard": "storage_volume_assets",
    "storage_volume_incarnations_lifecycle_guard": "storage_volume_incarnations",
    "storage_asset_coverage_gaps_lifecycle_guard": "storage_asset_coverage_gaps",
    "storage_asset_coverage_gaps_transition": "storage_asset_coverage_gaps",
    "storage_backend_assertions_append_only": "storage_backend_assertions",
    "storage_backend_assertions_transition": "storage_backend_assertions",
}
REQUIRED_SLICE2_VOLUME_APP_TRIGGERS = frozenset(
    REQUIRED_SLICE2_VOLUME_APP_TRIGGER_RELATIONS
)

REQUIRED_SLICE2_CLAIM_APP_CONSTRAINT_RELATIONS = {
    "storage_metering_activation_basis_check": "storage_metering_activation",
    "storage_metering_activation_state_check": "storage_metering_activation",
    "storage_shadow_observations_snapshot_fkey": "storage_shadow_observations",
    "storage_shadow_observations_snapshot_identity_uq": ("storage_shadow_observations"),
    "storage_shadow_observations_identity_check": "storage_shadow_observations",
    "storage_shadow_observations_shape_check": "storage_shadow_observations",
}
REQUIRED_SLICE2_CLAIM_APP_CONSTRAINTS = frozenset(
    REQUIRED_SLICE2_CLAIM_APP_CONSTRAINT_RELATIONS
)

REQUIRED_SLICE2_VOLUME_APP_CONSTRAINT_RELATIONS = {
    "infrastructure_storage_resource_mappings_pkey": (
        "infrastructure_storage_resource_mappings"
    ),
    "infrastructure_storage_resource_mappings_selector_uq": (
        "infrastructure_storage_resource_mappings"
    ),
    "infrastructure_storage_resource_mappings_selector_check": (
        "infrastructure_storage_resource_mappings"
    ),
    "infrastructure_storage_resource_mappings_output_check": (
        "infrastructure_storage_resource_mappings"
    ),
    "storage_identity_key_state_singleton_check": "storage_identity_key_state",
    "storage_identity_key_state_shape_check": "storage_identity_key_state",
    "storage_volume_assets_identity_uq": "storage_volume_assets",
    "storage_volume_assets_identity_check": "storage_volume_assets",
    "storage_volume_assets_time_check": "storage_volume_assets",
    "storage_volume_assets_state_check": "storage_volume_assets",
    "storage_volume_assets_destruction_assertion_fkey": "storage_volume_assets",
    "storage_volume_incarnations_scope_cluster_fkey": ("storage_volume_incarnations"),
    "storage_volume_incarnations_pv_uid_uq": "storage_volume_incarnations",
    "storage_volume_incarnations_identity_check": "storage_volume_incarnations",
    "storage_volume_incarnations_shape_check": "storage_volume_incarnations",
    "storage_volume_incarnations_no_overlap": "storage_volume_incarnations",
    "storage_asset_coverage_gaps_reason_check": "storage_asset_coverage_gaps",
    "storage_asset_coverage_gaps_range_check": "storage_asset_coverage_gaps",
    "storage_asset_coverage_gaps_resolution_check": "storage_asset_coverage_gaps",
    "storage_asset_coverage_gaps_no_overlap": "storage_asset_coverage_gaps",
    "storage_asset_coverage_gaps_assertion_fkey": "storage_asset_coverage_gaps",
    "storage_backend_assertions_idempotency_uq": "storage_backend_assertions",
    "storage_backend_assertions_asset_uq": "storage_backend_assertions",
    "storage_backend_assertions_shape_check": "storage_backend_assertions",
}
REQUIRED_SLICE2_VOLUME_APP_CONSTRAINTS = frozenset(
    REQUIRED_SLICE2_VOLUME_APP_CONSTRAINT_RELATIONS
)

# Slice 3 compute classes share one activation/journal migration, but remain
# independently gated at runtime. This probe establishes only schema safety;
# it never implies that any class is in shadow or active state.
REQUIRED_SLICE3_COMPUTE_APP_TABLES = frozenset(
    {
        "agent_metering_binding_events",
        "agent_metering_pod_identity_state",
        "compute_metering_activation",
        "compute_metering_epoch_authorities",
        "compute_metering_epoch_promotion_requests",
        "compute_metering_scope_requirements",
        "compute_shadow_observations",
    }
)

REQUIRED_SLICE3_COMPUTE_APP_INDEX_RELATIONS = {
    "agent_metering_binding_events_pod_time_idx": "agent_metering_binding_events",
    "agent_metering_binding_events_owner_time_idx": "agent_metering_binding_events",
    "agent_metering_pod_identity_state_pod_uid_idx": (
        "agent_metering_pod_identity_state"
    ),
    "agent_metering_pod_identity_state_owner_idx": (
        "agent_metering_pod_identity_state"
    ),
    "compute_shadow_observations_latest_idx": "compute_shadow_observations",
    "compute_shadow_observations_retention_idx": "compute_shadow_observations",
    "compute_metering_scope_requirements_scope_idx": (
        "compute_metering_scope_requirements"
    ),
    "compute_metering_scope_requirements_epoch_idx": (
        "compute_metering_scope_requirements"
    ),
    "compute_epoch_promotion_requests_activation_idx": (
        "compute_metering_epoch_promotion_requests"
    ),
    "compute_epoch_authorities_current_idx": "compute_metering_epoch_authorities",
    "compute_epoch_authorities_epoch_idx": "compute_metering_epoch_authorities",
    "resource_intervals_compute_scope_epoch_idx": "resource_intervals",
}
REQUIRED_SLICE3_COMPUTE_APP_INDEXES = frozenset(
    REQUIRED_SLICE3_COMPUTE_APP_INDEX_RELATIONS
)

REQUIRED_SLICE3_COMPUTE_APP_TRIGGER_RELATIONS = {
    "agent_metering_agents_delete": "agents",
    "agent_metering_agents_insert": "agents",
    "agent_metering_agents_update": "agents",
    "agent_metering_binding_events_append_only": "agent_metering_binding_events",
    "agent_metering_jobs_delete": "jobs",
    "agent_metering_jobs_insert": "jobs",
    "agent_metering_jobs_update": "jobs",
    "agent_metering_pod_identity_state_journal": ("agent_metering_pod_identity_state"),
    "agent_metering_pod_identity_state_one_way": ("agent_metering_pod_identity_state"),
    "agent_metering_threads_delete": "threads",
    "agent_metering_threads_insert": "threads",
    "agent_metering_threads_update": "threads",
    "compute_metering_activation_one_way": "compute_metering_activation",
    "compute_metering_scope_requirements_immutable": (
        "compute_metering_scope_requirements"
    ),
    "compute_shadow_observations_immutable": "compute_shadow_observations",
    "compute_epoch_promotion_requests_immutable": (
        "compute_metering_epoch_promotion_requests"
    ),
    "compute_metering_epoch_authorities_immutable": (
        "compute_metering_epoch_authorities"
    ),
    "resource_intervals_compute_epoch_authority_guard": "resource_intervals",
    "resource_inventory_epochs_compute_retirement": ("resource_inventory_scope_epochs"),
    "resource_inventory_epochs_recovery_identity_immutable": (
        "resource_inventory_scope_epochs"
    ),
}
REQUIRED_SLICE3_COMPUTE_APP_TRIGGERS = frozenset(
    REQUIRED_SLICE3_COMPUTE_APP_TRIGGER_RELATIONS
)

REQUIRED_SLICE3_COMPUTE_APP_CONSTRAINT_RELATIONS = {
    "agent_metering_binding_events_agent_revision_uq": (
        "agent_metering_binding_events"
    ),
    "agent_metering_binding_events_attribution_check": (
        "agent_metering_binding_events"
    ),
    "agent_metering_binding_events_identity_check": "agent_metering_binding_events",
    "agent_metering_pod_identity_state_attribution_check": (
        "agent_metering_pod_identity_state"
    ),
    "agent_metering_pod_identity_state_identity_check": (
        "agent_metering_pod_identity_state"
    ),
    "compute_metering_activation_key_check": "compute_metering_activation",
    "compute_metering_activation_state_check": "compute_metering_activation",
    "compute_metering_scope_requirements_activation_fkey": (
        "compute_metering_scope_requirements"
    ),
    "compute_metering_scope_requirements_boundary_check": (
        "compute_metering_scope_requirements"
    ),
    "compute_metering_scope_requirements_epoch_scope_fkey": (
        "compute_metering_scope_requirements"
    ),
    "compute_metering_scope_requirements_epoch_uq": (
        "compute_metering_scope_requirements"
    ),
    "compute_metering_scope_requirements_pkey": ("compute_metering_scope_requirements"),
    "compute_metering_scope_requirements_scope_fkey": (
        "compute_metering_scope_requirements"
    ),
    "compute_shadow_observations_attribution_check": "compute_shadow_observations",
    "compute_shadow_observations_capacity_check": "compute_shadow_observations",
    "compute_shadow_observations_identity_check": "compute_shadow_observations",
    "compute_shadow_observations_item_uq": "compute_shadow_observations",
    "compute_shadow_observations_snapshot_fkey": "compute_shadow_observations",
    "compute_epoch_promotion_requests_kind_check": (
        "compute_metering_epoch_promotion_requests"
    ),
    "compute_epoch_promotion_requests_identity_check": (
        "compute_metering_epoch_promotion_requests"
    ),
    "compute_metering_epoch_promotion_requests_pkey": (
        "compute_metering_epoch_promotion_requests"
    ),
    "compute_metering_epoch_promotion_requests_activation_key_fkey": (
        "compute_metering_epoch_promotion_requests"
    ),
    "compute_metering_epoch_authorities_pkey": ("compute_metering_epoch_authorities"),
    "compute_metering_epoch_authorities_activation_key_fkey": (
        "compute_metering_epoch_authorities"
    ),
    "compute_metering_epoch_authorities_promotion_request_id_fkey": (
        "compute_metering_epoch_authorities"
    ),
    "compute_epoch_authorities_scope_fkey": ("compute_metering_epoch_authorities"),
    "compute_epoch_authorities_epoch_scope_fkey": (
        "compute_metering_epoch_authorities"
    ),
    "compute_epoch_authorities_predecessor_scope_fkey": (
        "compute_metering_epoch_authorities"
    ),
    "compute_epoch_authorities_proof_epoch_fkey": (
        "compute_metering_epoch_authorities"
    ),
    "compute_epoch_authorities_proof_scope_fkey": (
        "compute_metering_epoch_authorities"
    ),
    "compute_epoch_authorities_id_scope_uq": ("compute_metering_epoch_authorities"),
    "compute_epoch_authorities_previous_fkey": ("compute_metering_epoch_authorities"),
    "compute_epoch_authorities_epoch_uq": ("compute_metering_epoch_authorities"),
    "compute_epoch_authorities_sequence_uq": ("compute_metering_epoch_authorities"),
    "compute_epoch_authorities_request_scope_uq": (
        "compute_metering_epoch_authorities"
    ),
    "compute_epoch_authorities_sequence_check": ("compute_metering_epoch_authorities"),
    "resource_inventory_scope_epochs_retirement_check": (
        "resource_inventory_scope_epochs"
    ),
    "resource_intervals_compute_scope_epoch_fkey": "resource_intervals",
    "resource_intervals_compute_scope_epoch_shape_check": "resource_intervals",
}
REQUIRED_SLICE3_COMPUTE_APP_CONSTRAINTS = frozenset(
    REQUIRED_SLICE3_COMPUTE_APP_CONSTRAINT_RELATIONS
)

REQUIRED_SLICE3_COMPUTE_APP_COLUMNS = frozenset(
    {"resource_intervals.compute_scope_epoch_id"}
)

# Slice 3B adds an exact, immutable set of storage authorities on top of the
# Slice 2 per-basis master. Remote PVC/PV inventory remains useful without this
# schema, but shadow reconciliation, lifecycle accrual, and publication must
# fail closed until every source is independently fenced.
REQUIRED_SLICE3_STORAGE_APP_TABLES = frozenset(
    {
        "storage_metering_source_activations",
        "storage_metering_source_requirements",
    }
)

REQUIRED_SLICE3_STORAGE_APP_INDEX_RELATIONS = {
    "storage_metering_source_requirements_scope_idx": (
        "storage_metering_source_requirements"
    ),
}
REQUIRED_SLICE3_STORAGE_APP_INDEXES = frozenset(
    REQUIRED_SLICE3_STORAGE_APP_INDEX_RELATIONS
)

REQUIRED_SLICE3_STORAGE_APP_TRIGGER_RELATIONS = {
    "storage_metering_source_activations_one_way": (
        "storage_metering_source_activations"
    ),
    "storage_metering_source_requirements_immutable": (
        "storage_metering_source_requirements"
    ),
}
REQUIRED_SLICE3_STORAGE_APP_TRIGGERS = frozenset(
    REQUIRED_SLICE3_STORAGE_APP_TRIGGER_RELATIONS
)

REQUIRED_SLICE3_STORAGE_APP_CONSTRAINT_RELATIONS = {
    "resource_inventory_scopes_source_identity_uq": "resource_inventory_scopes",
    "storage_metering_source_activations_pkey": ("storage_metering_source_activations"),
    "storage_metering_source_activations_identity_check": (
        "storage_metering_source_activations"
    ),
    "storage_metering_source_activations_state_check": (
        "storage_metering_source_activations"
    ),
    "storage_metering_source_requirements_pkey": (
        "storage_metering_source_requirements"
    ),
    "storage_metering_source_requirements_activation_fkey": (
        "storage_metering_source_requirements"
    ),
    "storage_metering_source_requirements_scope_fkey": (
        "storage_metering_source_requirements"
    ),
    "storage_metering_source_requirements_role_check": (
        "storage_metering_source_requirements"
    ),
}
REQUIRED_SLICE3_STORAGE_APP_CONSTRAINTS = frozenset(
    REQUIRED_SLICE3_STORAGE_APP_CONSTRAINT_RELATIONS
)

REQUIRED_AUDIT_TRIGGER_RELATIONS = {
    "usage_events_rollup_dirty_days": "usage_events",
    "usage_events_append_only_v2": "usage_events",
}

REQUIRED_AUDIT_INDEX_RELATIONS = {
    "usage_events_dedupe_idx": "usage_events",
    "usage_events_project_ts_idx": "usage_events",
    "usage_rollup_dirty_days_pkey": "usage_rollup_dirty_days",
}
REQUIRED_AUDIT_INDEXES = frozenset(REQUIRED_AUDIT_INDEX_RELATIONS)

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
    app_columns: frozenset[str] = frozenset()
    app_constraints: frozenset[str] = frozenset()
    audit_tables: frozenset[str] = frozenset()
    audit_columns: frozenset[str] = frozenset()
    audit_constraints: frozenset[str] = frozenset()
    audit_indexes: frozenset[str] = frozenset()
    app_seed_rows_ready: bool = False
    half_even_function: bool = False
    dirty_day_trigger: bool = False
    append_only_trigger: bool = False
    target_partitions_ready: bool = False
    storage_activation_seed_rows_ready: bool = False
    compute_activation_seed_rows_ready: bool = False
    storage_identity_key_registered: bool = False
    storage_identity_key_version: str | None = None

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
    def missing_slice1_runtime_app_tables(self) -> frozenset[str]:
        return REQUIRED_SLICE1_RUNTIME_APP_TABLES - self.app_tables

    @property
    def missing_slice1_runtime_app_indexes(self) -> frozenset[str]:
        return REQUIRED_SLICE1_RUNTIME_APP_INDEXES - self.app_indexes

    @property
    def missing_slice1_runtime_app_triggers(self) -> frozenset[str]:
        return REQUIRED_SLICE1_RUNTIME_APP_TRIGGERS - self.app_triggers

    @property
    def missing_slice1_runtime_app_columns(self) -> frozenset[str]:
        return REQUIRED_SLICE1_RUNTIME_APP_COLUMNS - self.app_columns

    @property
    def missing_slice1_runtime_app_constraints(self) -> frozenset[str]:
        return REQUIRED_SLICE1_RUNTIME_APP_CONSTRAINTS - self.app_constraints

    @property
    def missing_slice2_claim_app_tables(self) -> frozenset[str]:
        return REQUIRED_SLICE2_CLAIM_APP_TABLES - self.app_tables

    @property
    def missing_slice2_claim_app_indexes(self) -> frozenset[str]:
        return REQUIRED_SLICE2_CLAIM_APP_INDEXES - self.app_indexes

    @property
    def missing_slice2_claim_app_triggers(self) -> frozenset[str]:
        return REQUIRED_SLICE2_CLAIM_APP_TRIGGERS - self.app_triggers

    @property
    def missing_slice2_claim_app_constraints(self) -> frozenset[str]:
        return REQUIRED_SLICE2_CLAIM_APP_CONSTRAINTS - self.app_constraints

    @property
    def missing_slice2_volume_app_tables(self) -> frozenset[str]:
        return REQUIRED_SLICE2_VOLUME_APP_TABLES - self.app_tables

    @property
    def missing_slice2_volume_app_indexes(self) -> frozenset[str]:
        return REQUIRED_SLICE2_VOLUME_APP_INDEXES - self.app_indexes

    @property
    def missing_slice2_volume_app_triggers(self) -> frozenset[str]:
        return REQUIRED_SLICE2_VOLUME_APP_TRIGGERS - self.app_triggers

    @property
    def missing_slice2_volume_app_constraints(self) -> frozenset[str]:
        return REQUIRED_SLICE2_VOLUME_APP_CONSTRAINTS - self.app_constraints

    @property
    def missing_slice3_compute_app_tables(self) -> frozenset[str]:
        return REQUIRED_SLICE3_COMPUTE_APP_TABLES - self.app_tables

    @property
    def missing_slice3_compute_app_indexes(self) -> frozenset[str]:
        return REQUIRED_SLICE3_COMPUTE_APP_INDEXES - self.app_indexes

    @property
    def missing_slice3_compute_app_triggers(self) -> frozenset[str]:
        return REQUIRED_SLICE3_COMPUTE_APP_TRIGGERS - self.app_triggers

    @property
    def missing_slice3_compute_app_constraints(self) -> frozenset[str]:
        return REQUIRED_SLICE3_COMPUTE_APP_CONSTRAINTS - self.app_constraints

    @property
    def missing_slice3_compute_app_columns(self) -> frozenset[str]:
        return REQUIRED_SLICE3_COMPUTE_APP_COLUMNS - self.app_columns

    @property
    def missing_slice3_storage_app_tables(self) -> frozenset[str]:
        return REQUIRED_SLICE3_STORAGE_APP_TABLES - self.app_tables

    @property
    def missing_slice3_storage_app_indexes(self) -> frozenset[str]:
        return REQUIRED_SLICE3_STORAGE_APP_INDEXES - self.app_indexes

    @property
    def missing_slice3_storage_app_triggers(self) -> frozenset[str]:
        return REQUIRED_SLICE3_STORAGE_APP_TRIGGERS - self.app_triggers

    @property
    def missing_slice3_storage_app_constraints(self) -> frozenset[str]:
        return REQUIRED_SLICE3_STORAGE_APP_CONSTRAINTS - self.app_constraints

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

    @property
    def slice1_runtime_ready(self) -> bool:
        """Full audit/publication/cutover contract introduced through 0101."""

        return (
            self.slice0_ready
            and self.slice1_inventory_ready
            and not self.missing_slice1_runtime_app_tables
            and not self.missing_slice1_runtime_app_indexes
            and not self.missing_slice1_runtime_app_triggers
            and not self.missing_slice1_runtime_app_columns
            and not self.missing_slice1_runtime_app_constraints
        )

    @property
    def slice2_claim_inventory_ready(self) -> bool:
        """Namespaced claim shadow/inventory schema, without publication."""

        return (
            self.slice1_inventory_ready
            and not self.missing_slice2_claim_app_tables
            and not self.missing_slice2_claim_app_indexes
            and not self.missing_slice2_claim_app_triggers
            and not self.missing_slice2_claim_app_constraints
            and self.storage_activation_seed_rows_ready
        )

    @property
    def slice2_volume_schema_ready(self) -> bool:
        """Cluster-volume schema readiness, independent of key registration."""

        return (
            self.slice2_claim_inventory_ready
            and not self.missing_slice2_volume_app_tables
            and not self.missing_slice2_volume_app_indexes
            and not self.missing_slice2_volume_app_triggers
            and not self.missing_slice2_volume_app_constraints
        )

    @property
    def slice2_volume_inventory_ready(self) -> bool:
        """Volume schema plus registered stable identity-key provenance."""

        return self.slice2_volume_schema_ready and self.storage_identity_key_registered

    @property
    def slice3_compute_inventory_ready(self) -> bool:
        """Compute activation, shadow, and agent identity journal readiness."""

        return (
            self.slice1_inventory_ready
            and not self.missing_slice3_compute_app_tables
            and not self.missing_slice3_compute_app_indexes
            and not self.missing_slice3_compute_app_triggers
            and not self.missing_slice3_compute_app_constraints
            and not self.missing_slice3_compute_app_columns
            and self.compute_activation_seed_rows_ready
        )

    @property
    def slice3_storage_lifecycle_ready(self) -> bool:
        """Exact per-source storage shadow/lifecycle/publication contract."""

        return (
            self.slice2_volume_schema_ready
            and not self.missing_slice3_storage_app_tables
            and not self.missing_slice3_storage_app_indexes
            and not self.missing_slice3_storage_app_triggers
            and not self.missing_slice3_storage_app_constraints
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
            "slice1_runtime_ready": self.slice1_runtime_ready,
            "missing_slice1_app_tables": sorted(self.missing_slice1_app_tables),
            "missing_slice1_app_indexes": sorted(self.missing_slice1_app_indexes),
            "missing_slice1_app_triggers": sorted(self.missing_slice1_app_triggers),
            "missing_slice1_runtime_app_tables": sorted(
                self.missing_slice1_runtime_app_tables
            ),
            "missing_slice1_runtime_app_indexes": sorted(
                self.missing_slice1_runtime_app_indexes
            ),
            "missing_slice1_runtime_app_triggers": sorted(
                self.missing_slice1_runtime_app_triggers
            ),
            "missing_slice1_runtime_app_columns": sorted(
                self.missing_slice1_runtime_app_columns
            ),
            "missing_slice1_runtime_app_constraints": sorted(
                self.missing_slice1_runtime_app_constraints
            ),
            "slice2_claim_inventory_ready": self.slice2_claim_inventory_ready,
            "slice2_volume_schema_ready": self.slice2_volume_schema_ready,
            "slice2_volume_inventory_ready": self.slice2_volume_inventory_ready,
            "storage_activation_seed_rows_ready": (
                self.storage_activation_seed_rows_ready
            ),
            "storage_identity_key_registered": self.storage_identity_key_registered,
            "storage_identity_key_version": self.storage_identity_key_version,
            "slice3_compute_inventory_ready": self.slice3_compute_inventory_ready,
            "slice3_storage_lifecycle_ready": self.slice3_storage_lifecycle_ready,
            "compute_activation_seed_rows_ready": (
                self.compute_activation_seed_rows_ready
            ),
            "missing_slice3_compute_app_tables": sorted(
                self.missing_slice3_compute_app_tables
            ),
            "missing_slice3_compute_app_indexes": sorted(
                self.missing_slice3_compute_app_indexes
            ),
            "missing_slice3_compute_app_triggers": sorted(
                self.missing_slice3_compute_app_triggers
            ),
            "missing_slice3_compute_app_constraints": sorted(
                self.missing_slice3_compute_app_constraints
            ),
            "missing_slice3_compute_app_columns": sorted(
                self.missing_slice3_compute_app_columns
            ),
            "missing_slice3_storage_app_tables": sorted(
                self.missing_slice3_storage_app_tables
            ),
            "missing_slice3_storage_app_indexes": sorted(
                self.missing_slice3_storage_app_indexes
            ),
            "missing_slice3_storage_app_triggers": sorted(
                self.missing_slice3_storage_app_triggers
            ),
            "missing_slice3_storage_app_constraints": sorted(
                self.missing_slice3_storage_app_constraints
            ),
            "missing_slice2_claim_app_tables": sorted(
                self.missing_slice2_claim_app_tables
            ),
            "missing_slice2_claim_app_indexes": sorted(
                self.missing_slice2_claim_app_indexes
            ),
            "missing_slice2_claim_app_triggers": sorted(
                self.missing_slice2_claim_app_triggers
            ),
            "missing_slice2_claim_app_constraints": sorted(
                self.missing_slice2_claim_app_constraints
            ),
            "missing_slice2_volume_app_tables": sorted(
                self.missing_slice2_volume_app_tables
            ),
            "missing_slice2_volume_app_indexes": sorted(
                self.missing_slice2_volume_app_indexes
            ),
            "missing_slice2_volume_app_triggers": sorted(
                self.missing_slice2_volume_app_triggers
            ),
            "missing_slice2_volume_app_constraints": sorted(
                self.missing_slice2_volume_app_constraints
            ),
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


async def _qualified_column_names(
    pool: asyncpg.Pool | None,
    wanted: frozenset[str],
) -> set[str]:
    if pool is None or not wanted:
        return set()
    tables = sorted({item.split(".", 1)[0] for item in wanted})
    try:
        rows = await pool.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ANY($1::text[])",
            tables,
        )
    except Exception:
        logger.warning("metering schema app-column probe failed", exc_info=True)
        return set()
    return {
        f"{row['table_name']}.{row['column_name']}"
        for row in rows
        if f"{row['table_name']}.{row['column_name']}" in wanted
    }


async def _index_names(
    pool: asyncpg.Pool | None,
    wanted: Mapping[str, str],
) -> set[str]:
    if pool is None:
        return set()
    try:
        rows = await pool.fetch(
            "SELECT index_relation.relname AS indexname, "
            "indexed_relation.relname AS tablename "
            "FROM pg_catalog.pg_index AS index_state "
            "JOIN pg_catalog.pg_class AS index_relation "
            "ON index_relation.oid = index_state.indexrelid "
            "JOIN pg_catalog.pg_namespace AS index_namespace "
            "ON index_namespace.oid = index_relation.relnamespace "
            "JOIN pg_catalog.pg_class AS indexed_relation "
            "ON indexed_relation.oid = index_state.indrelid "
            "JOIN pg_catalog.pg_namespace AS indexed_namespace "
            "ON indexed_namespace.oid = indexed_relation.relnamespace "
            "WHERE index_namespace.nspname = 'public' "
            "AND indexed_namespace.nspname = 'public' "
            "AND index_relation.relname = ANY($1::text[]) "
            "AND index_state.indisvalid "
            "AND index_state.indisready "
            "AND index_state.indislive",
            list(wanted),
        )
    except Exception:
        logger.warning("metering schema index probe failed", exc_info=True)
        return set()
    return {
        str(row["indexname"])
        for row in rows
        if str(row["tablename"]) == wanted.get(str(row["indexname"]))
    }


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


async def _validated_constraint_names(
    pool: asyncpg.Pool | None,
    wanted: Mapping[str, str],
) -> set[str]:
    if pool is None:
        return set()
    try:
        rows = await pool.fetch(
            "SELECT constraint_state.conname, relation.relname "
            "FROM pg_catalog.pg_constraint AS constraint_state "
            "JOIN pg_catalog.pg_class AS relation "
            "ON relation.oid = constraint_state.conrelid "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = 'public' "
            "AND constraint_state.convalidated "
            "AND constraint_state.conname = ANY($1::text[])",
            list(wanted),
        )
    except Exception:
        logger.warning("metering schema app-constraint probe failed", exc_info=True)
        return set()
    return {
        str(row["conname"])
        for row in rows
        if str(row["relname"]) == wanted.get(str(row["conname"]))
    }


async def probe_schema_capabilities(
    app_pool: asyncpg.Pool | None,
    audit_pool: asyncpg.Pool | None,
) -> MeteringSchemaCapabilities:
    """Probe both databases; absence remains a normal, fail-closed state."""
    app_tables = await _table_names(
        app_pool,
        REQUIRED_APP_TABLES
        | REQUIRED_SLICE1_APP_TABLES
        | REQUIRED_SLICE1_RUNTIME_APP_TABLES
        | REQUIRED_SLICE2_CLAIM_APP_TABLES
        | REQUIRED_SLICE2_VOLUME_APP_TABLES
        | REQUIRED_SLICE3_COMPUTE_APP_TABLES
        | REQUIRED_SLICE3_STORAGE_APP_TABLES,
    )
    app_indexes = await _index_names(
        app_pool,
        REQUIRED_APP_INDEX_RELATIONS
        | REQUIRED_SLICE1_APP_INDEX_RELATIONS
        | REQUIRED_SLICE1_RUNTIME_APP_INDEX_RELATIONS
        | REQUIRED_SLICE2_CLAIM_APP_INDEX_RELATIONS
        | REQUIRED_SLICE2_VOLUME_APP_INDEX_RELATIONS
        | REQUIRED_SLICE3_COMPUTE_APP_INDEX_RELATIONS
        | REQUIRED_SLICE3_STORAGE_APP_INDEX_RELATIONS,
    )
    app_triggers = await _enabled_trigger_names(
        app_pool,
        REQUIRED_APP_TRIGGER_RELATIONS
        | REQUIRED_SLICE1_APP_TRIGGER_RELATIONS
        | REQUIRED_SLICE1_RUNTIME_APP_TRIGGER_RELATIONS
        | REQUIRED_SLICE2_CLAIM_APP_TRIGGER_RELATIONS
        | REQUIRED_SLICE2_VOLUME_APP_TRIGGER_RELATIONS
        | REQUIRED_SLICE3_COMPUTE_APP_TRIGGER_RELATIONS
        | REQUIRED_SLICE3_STORAGE_APP_TRIGGER_RELATIONS,
    )
    app_columns = await _qualified_column_names(
        app_pool,
        REQUIRED_SLICE1_RUNTIME_APP_COLUMNS | REQUIRED_SLICE3_COMPUTE_APP_COLUMNS,
    )
    app_constraints = await _validated_constraint_names(
        app_pool,
        REQUIRED_SLICE1_RUNTIME_APP_CONSTRAINT_RELATIONS
        | REQUIRED_SLICE2_CLAIM_APP_CONSTRAINT_RELATIONS
        | REQUIRED_SLICE2_VOLUME_APP_CONSTRAINT_RELATIONS
        | REQUIRED_SLICE3_COMPUTE_APP_CONSTRAINT_RELATIONS
        | REQUIRED_SLICE3_STORAGE_APP_CONSTRAINT_RELATIONS,
    )
    audit_tables = await _table_names(audit_pool, REQUIRED_AUDIT_TABLES)
    audit_indexes = await _index_names(audit_pool, REQUIRED_AUDIT_INDEX_RELATIONS)
    audit_columns: set[str] = set()
    audit_constraints: set[str] = set()
    app_seed_rows_ready = False
    half_even_function = False
    dirty_trigger = False
    append_only_trigger = False
    target_partitions_ready = False
    storage_activation_seed_rows_ready = False
    compute_activation_seed_rows_ready = False
    storage_identity_key_registered = False
    storage_identity_key_version: str | None = None
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
    if app_pool is not None and "storage_metering_activation" in app_tables:
        try:
            storage_activation_seed_rows_ready = bool(
                await app_pool.fetchval(
                    "SELECT count(*) = 2 "
                    "AND count(*) FILTER (WHERE measurement_basis = "
                    "'claim-requested') = 1 "
                    "AND count(*) FILTER (WHERE measurement_basis = "
                    "'volume-provisioned') = 1 "
                    "FROM storage_metering_activation"
                )
            )
        except Exception:
            logger.warning(
                "metering storage activation seed probe failed", exc_info=True
            )
    if app_pool is not None and "compute_metering_activation" in app_tables:
        try:
            compute_activation_seed_rows_ready = bool(
                await app_pool.fetchval(
                    "SELECT count(*) = 3 "
                    "AND count(*) FILTER (WHERE activation_key='agent_pod') = 1 "
                    "AND count(*) FILTER (WHERE activation_key="
                    "'ide_workspace_pod') = 1 "
                    "AND count(*) FILTER (WHERE activation_key='workspace_vm') = 1 "
                    "FROM compute_metering_activation"
                )
            )
        except Exception:
            logger.warning(
                "metering compute activation seed probe failed", exc_info=True
            )
    if app_pool is not None and "storage_identity_key_state" in app_tables:
        try:
            identity_key = await app_pool.fetchrow(
                "SELECT key_version FROM storage_identity_key_state "
                "WHERE singleton AND algorithm = 'hmac-sha256-v1'"
            )
            storage_identity_key_registered = identity_key is not None
            if identity_key is not None:
                storage_identity_key_version = str(identity_key["key_version"])
        except Exception:
            logger.warning("metering storage identity-key probe failed", exc_info=True)
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
        app_columns=frozenset(app_columns),
        app_constraints=frozenset(app_constraints),
        audit_tables=frozenset(audit_tables),
        audit_columns=frozenset(audit_columns),
        audit_constraints=frozenset(audit_constraints),
        audit_indexes=frozenset(audit_indexes),
        app_seed_rows_ready=app_seed_rows_ready,
        half_even_function=half_even_function,
        dirty_day_trigger=dirty_trigger,
        append_only_trigger=append_only_trigger,
        target_partitions_ready=target_partitions_ready,
        storage_activation_seed_rows_ready=storage_activation_seed_rows_ready,
        compute_activation_seed_rows_ready=compute_activation_seed_rows_ready,
        storage_identity_key_registered=storage_identity_key_registered,
        storage_identity_key_version=storage_identity_key_version,
    )
