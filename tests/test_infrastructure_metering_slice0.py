from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from orchestrator.services.infrastructure_metering.capabilities import (
    REQUIRED_APP_INDEX_RELATIONS,
    REQUIRED_APP_TABLES,
    REQUIRED_APP_TRIGGER_RELATIONS,
    REQUIRED_APP_TRIGGERS,
    REQUIRED_AUDIT_INDEX_RELATIONS,
    REQUIRED_SLICE1_APP_INDEX_RELATIONS,
    REQUIRED_SLICE1_APP_TABLES,
    REQUIRED_SLICE1_APP_TRIGGER_RELATIONS,
    REQUIRED_SLICE1_APP_TRIGGERS,
    REQUIRED_SLICE1_RUNTIME_APP_COLUMNS,
    REQUIRED_SLICE1_RUNTIME_APP_CONSTRAINT_RELATIONS,
    REQUIRED_SLICE1_RUNTIME_APP_INDEX_RELATIONS,
    REQUIRED_SLICE1_RUNTIME_APP_TABLES,
    REQUIRED_SLICE1_RUNTIME_APP_TRIGGER_RELATIONS,
    REQUIRED_AUDIT_COLUMNS,
    REQUIRED_AUDIT_CONSTRAINTS,
    REQUIRED_AUDIT_INDEXES,
    REQUIRED_AUDIT_TABLES,
    MeteringSchemaCapabilities,
    probe_schema_capabilities,
)
from orchestrator.services.infrastructure_metering.config import (
    InfrastructureMeteringSettings,
)
from orchestrator.services.infrastructure_metering.queries import (
    UsageV2QueryService,
    UsageVisibility,
)
from orchestrator.services.infrastructure_metering.types import (
    UsageCoverageV2,
    UsageLedgerCostV2,
    UsageRowV2,
    UsageWindowV2,
    decimal_text,
    ledger_cost,
)
from orchestrator.services.usage_ledger import UsageLedger, UsageRates


def _read_capabilities() -> MeteringSchemaCapabilities:
    return MeteringSchemaCapabilities(
        audit_tables=REQUIRED_AUDIT_TABLES,
        audit_columns=REQUIRED_AUDIT_COLUMNS,
        audit_constraints=REQUIRED_AUDIT_CONSTRAINTS,
        audit_indexes=REQUIRED_AUDIT_INDEXES,
        half_even_function=True,
        dirty_day_trigger=True,
        append_only_trigger=True,
    )


def test_cutover_wiring_uses_configured_inventory_freshness() -> None:
    import main as orchestrator_main

    source = inspect.getsource(orchestrator_main.lifespan)
    assert "max_scope_age=timedelta(" in source
    assert "seconds=infrastructure_metering_settings.stale_after_seconds" in source
    assert "max_collector_clock_skew=timedelta(" in source
    assert "infrastructure_metering_settings.max_collector_clock_skew_seconds" in source


def test_storage_publication_resources_require_effective_activation() -> None:
    import main as orchestrator_main
    from orchestrator.services.infrastructure_metering.storage_assets import (
        StorageActivation,
    )

    settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=True,
        publication_enabled=True,
        pvc_inventory_enabled=True,
        pv_inventory_enabled=True,
        pvc_shadow_enabled=True,
        pv_shadow_enabled=True,
        pvc_publication_enabled=True,
        pv_publication_enabled=True,
        volume_identity_key_version="storage-v1",
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
    )
    capabilities = MagicMock(
        slice2_claim_inventory_ready=True,
        slice2_volume_inventory_ready=True,
        slice3_storage_lifecycle_ready=True,
        storage_identity_key_version="storage-v1",
    )
    boundary = datetime(2026, 8, 7, tzinfo=timezone.utc)
    before = StorageActivation(
        measurement_basis="claim-requested",
        state="active",
        activated_at=boundary,
        database_time=boundary - timedelta(microseconds=1),
    )
    effective_claim = StorageActivation(
        measurement_basis="claim-requested",
        state="active",
        activated_at=boundary,
        database_time=boundary,
    )
    effective_volume = StorageActivation(
        measurement_basis="volume-provisioned",
        state="active",
        activated_at=boundary,
        database_time=boundary + timedelta(hours=1),
    )
    before_source = orchestrator_main.StorageSourceActivation(
        measurement_basis="claim-requested",
        collector_id="kubernetes-pods",
        source_cluster="dev-cluster",
        state="active",
        activated_at=boundary,
        database_time=boundary - timedelta(microseconds=1),
    )
    effective_claim_source = orchestrator_main.StorageSourceActivation(
        measurement_basis="claim-requested",
        collector_id="kubernetes-pods",
        source_cluster="dev-cluster",
        state="active",
        activated_at=boundary,
        database_time=boundary,
    )
    effective_volume_source = orchestrator_main.StorageSourceActivation(
        measurement_basis="volume-provisioned",
        collector_id="kubernetes-pods",
        source_cluster="dev-cluster",
        state="active",
        activated_at=boundary,
        database_time=boundary + timedelta(hours=1),
    )
    requested_policy = orchestrator_main._requested_storage_publication_policy(settings)

    before_policy = orchestrator_main._capability_gated_storage_publication_policy(
        requested_policy,
        capabilities,
        claim_activation=before,
        volume_activation=effective_volume,
        source_activations={
            ("claim-requested", "kubernetes-pods", "dev-cluster"): before_source,
        },
        volume_mapping_ready=True,
        volume_identity_key_matches=True,
    )
    assert before_policy.authorities == ()
    enabled_policy = orchestrator_main._capability_gated_storage_publication_policy(
        requested_policy,
        capabilities,
        claim_activation=effective_claim,
        volume_activation=effective_volume,
        source_activations={
            (
                "claim-requested",
                "kubernetes-pods",
                "dev-cluster",
            ): effective_claim_source,
            (
                "volume-provisioned",
                "kubernetes-pods",
                "dev-cluster",
            ): effective_volume_source,
        },
        volume_mapping_ready=True,
        volume_identity_key_matches=True,
    )
    enabled = orchestrator_main._capability_gated_infrastructure_publication_resources(
        settings,
        capabilities,
        mapped_volume_resources=("block_volume_longhorn_ephemeral",),
        storage_publication_policy=enabled_policy,
    )
    assert enabled == (
        "workspace_pod",
        *orchestrator_main._INFRASTRUCTURE_PVC_RESOURCES,
        *orchestrator_main._INFRASTRUCTURE_PV_RESOURCES,
        "block_volume_longhorn_ephemeral",
    )
    capabilities.storage_identity_key_version = "another-key"
    assert orchestrator_main._capability_gated_infrastructure_publication_resources(
        settings,
        capabilities,
        mapped_volume_resources=("block_volume_longhorn_ephemeral",),
        storage_publication_policy=enabled_policy,
    ) == ("workspace_pod", *orchestrator_main._INFRASTRUCTURE_PVC_RESOURCES)


def test_durable_reporting_policy_survives_write_gate_and_auth_loss() -> None:
    import main as orchestrator_main

    boundary = datetime(2026, 8, 8, tzinfo=timezone.utc)
    before_boundary = boundary - timedelta(hours=1)
    claim = orchestrator_main.StorageActivation(
        measurement_basis="claim-requested",
        state="active",
        activated_at=boundary,
        database_time=before_boundary,
    )
    volume = orchestrator_main.StorageActivation(
        measurement_basis="volume-provisioned",
        state="active",
        activated_at=boundary,
        database_time=before_boundary,
    )
    sources = (
        orchestrator_main.StorageSourceActivation(
            measurement_basis="claim-requested",
            collector_id="kubernetes-pods",
            source_cluster="main-dev",
            state="active",
            activated_at=boundary,
            database_time=before_boundary,
        ),
        orchestrator_main.StorageSourceActivation(
            measurement_basis="volume-provisioned",
            collector_id="kubevirt-storage",
            source_cluster="vm-cluster",
            state="active",
            activated_at=boundary,
            database_time=before_boundary,
        ),
    )
    storage_policy = orchestrator_main._durable_storage_reporting_policy(
        claim_activation=claim,
        volume_activation=volume,
        source_activations=sources,
    )
    assert storage_policy.authorities == (
        orchestrator_main.StoragePublicationAuthority(
            "claim-requested", "kubernetes-pods", "main-dev"
        ),
        orchestrator_main.StoragePublicationAuthority(
            "volume-provisioned", "kubevirt-storage", "vm-cluster"
        ),
    )

    compute = {
        key: orchestrator_main.ComputeActivation(
            activation_key=key,
            state="active",
            activated_at=boundary,
            database_time=before_boundary,
        )
        for key in ("agent_pod", "ide_workspace_pod", "workspace_vm")
    }
    capabilities = MagicMock(
        slice3_compute_inventory_ready=True,
        slice3_storage_lifecycle_ready=True,
        slice2_volume_inventory_ready=True,
    )
    resources = orchestrator_main._durable_infrastructure_reporting_resources(
        capabilities,
        mapped_volume_resources=("block_volume_stackit",),
        compute_activations=compute,
        storage_reporting_policy=storage_policy,
    )
    assert resources == (
        "workspace_pod",
        "agent_pod",
        "workspace_vm",
        *orchestrator_main._INFRASTRUCTURE_PVC_RESOURCES,
        *orchestrator_main._INFRASTRUCTURE_PV_RESOURCES,
        "block_volume_stackit",
    )
    assert orchestrator_main._compute_activation_is_durable(
        compute["ide_workspace_pod"]
    )

    # Current write controls remain independently dark and unauthenticated.
    settings = InfrastructureMeteringSettings()
    assert orchestrator_main._enabled_infrastructure_publication_resources(
        settings,
        mapped_volume_resources=("block_volume_stackit",),
    ) == ("workspace_pod",)
    assert (
        orchestrator_main._requested_storage_publication_policy(
            settings,
            vm_lifecycle_authenticated=False,
        ).authorities
        == ()
    )


def test_durable_volume_reporting_fails_closed_without_mapping_registry() -> None:
    import main as orchestrator_main

    capabilities = MagicMock(
        slice3_compute_inventory_ready=True,
        slice3_storage_lifecycle_ready=True,
        slice2_volume_inventory_ready=True,
    )
    policy = orchestrator_main.StoragePublicationPolicy(
        (
            orchestrator_main.StoragePublicationAuthority(
                "volume-provisioned", "kubernetes-pods", "main-dev"
            ),
        )
    )
    with pytest.raises(ValueError, match="mapping registry"):
        orchestrator_main._durable_infrastructure_reporting_resources(
            capabilities,
            volume_mapping_ready=False,
            storage_reporting_policy=policy,
        )


def test_storage_shadow_configuration_must_match_frozen_source_scopes() -> None:
    import main as orchestrator_main
    from services.infrastructure_metering.storage_assets import (
        StorageSourceRequirement,
    )

    settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=True,
        pvc_inventory_enabled=True,
        pvc_shadow_enabled=True,
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
    )
    assert orchestrator_main._storage_source_configuration_errors(settings, ()) == (
        "primary/claim-requested durable source shadow activation",
    )

    activation = orchestrator_main.StorageSourceActivation(
        measurement_basis="claim-requested",
        collector_id="kubernetes-pods",
        source_cluster="dev-cluster",
        state="shadow",
        activated_at=None,
        requirements=(
            StorageSourceRequirement(
                inventory_scope_id=uuid4(),
                api_resource="core/v1/persistentvolumeclaims",
                namespace="srw",
                requirement_role="quantity",
            ),
        ),
        database_time=datetime(2026, 8, 7, tzinfo=timezone.utc),
    )
    assert (
        orchestrator_main._storage_source_configuration_errors(
            settings,
            (activation,),
        )
        == ()
    )

    expanded = replace(settings, namespace_allowlist=("srw", "new-namespace"))
    assert orchestrator_main._storage_source_configuration_errors(
        expanded,
        (activation,),
    ) == ("primary/claim-requested frozen inventory scope set",)


def _source_aware_capabilities(*, slice1: bool) -> MeteringSchemaCapabilities:
    return MeteringSchemaCapabilities(
        app_tables=(
            REQUIRED_APP_TABLES
            | (
                REQUIRED_SLICE1_APP_TABLES | REQUIRED_SLICE1_RUNTIME_APP_TABLES
                if slice1
                else frozenset()
            )
        ),
        app_indexes=(
            frozenset(REQUIRED_APP_INDEX_RELATIONS)
            | (
                frozenset(REQUIRED_SLICE1_APP_INDEX_RELATIONS)
                | frozenset(REQUIRED_SLICE1_RUNTIME_APP_INDEX_RELATIONS)
                if slice1
                else frozenset()
            )
        ),
        app_triggers=(
            REQUIRED_APP_TRIGGERS
            | (
                REQUIRED_SLICE1_APP_TRIGGERS
                | frozenset(REQUIRED_SLICE1_RUNTIME_APP_TRIGGER_RELATIONS)
                if slice1
                else frozenset()
            )
        ),
        app_columns=(REQUIRED_SLICE1_RUNTIME_APP_COLUMNS if slice1 else frozenset()),
        app_constraints=(
            frozenset(REQUIRED_SLICE1_RUNTIME_APP_CONSTRAINT_RELATIONS)
            if slice1
            else frozenset()
        ),
        audit_tables=REQUIRED_AUDIT_TABLES,
        audit_columns=REQUIRED_AUDIT_COLUMNS,
        audit_constraints=REQUIRED_AUDIT_CONSTRAINTS,
        audit_indexes=REQUIRED_AUDIT_INDEXES,
        app_seed_rows_ready=True,
        half_even_function=True,
        dirty_day_trigger=True,
        append_only_trigger=True,
        target_partitions_ready=True,
    )


class _CapabilityPool:
    def __init__(
        self,
        *,
        app: bool,
        append_mode: str = "O",
        slice1: bool = False,
        slice1_runtime: bool = False,
        unusable_indexes: set[str] | None = None,
        wrong_relation_indexes: set[str] | None = None,
    ):
        self.app = app
        self.append_mode = append_mode
        self.slice1 = slice1
        self.slice1_runtime = slice1_runtime
        self.unusable_indexes = unusable_indexes or set()
        self.wrong_relation_indexes = wrong_relation_indexes or set()

    async def fetch(self, sql, *params):
        wanted = set(params[0]) if params else set()
        if "information_schema.tables" in sql:
            present = REQUIRED_APP_TABLES if self.app else REQUIRED_AUDIT_TABLES
            if self.app and self.slice1:
                present |= REQUIRED_SLICE1_APP_TABLES
            if self.app and self.slice1_runtime:
                present |= REQUIRED_SLICE1_RUNTIME_APP_TABLES
            return [{"table_name": name} for name in wanted & present]
        if "FROM pg_catalog.pg_index" in sql:
            assert "index_state.indisvalid" in sql
            assert "index_state.indisready" in sql
            assert "index_state.indislive" in sql
            relations = (
                dict(REQUIRED_APP_INDEX_RELATIONS)
                if self.app
                else dict(REQUIRED_AUDIT_INDEX_RELATIONS)
            )
            if self.app and self.slice1:
                relations.update(REQUIRED_SLICE1_APP_INDEX_RELATIONS)
            if self.app and self.slice1_runtime:
                relations.update(REQUIRED_SLICE1_RUNTIME_APP_INDEX_RELATIONS)
            return [
                {
                    "indexname": name,
                    "tablename": (
                        "wrong_relation"
                        if name in self.wrong_relation_indexes
                        else relations[name]
                    ),
                }
                for name in wanted & set(relations) - self.unusable_indexes
            ]
        if "information_schema.columns" in sql:
            if self.app:
                if not self.slice1_runtime:
                    return []
                return [
                    {"table_name": table, "column_name": column}
                    for item in REQUIRED_SLICE1_RUNTIME_APP_COLUMNS
                    for table, column in [item.split(".", 1)]
                    if table in wanted
                ]
            return [{"column_name": name} for name in wanted & REQUIRED_AUDIT_COLUMNS]
        if "pg_constraint" in sql:
            if self.app:
                if not self.slice1_runtime:
                    return []
                return [
                    {"conname": name, "relname": relation}
                    for name, relation in (
                        REQUIRED_SLICE1_RUNTIME_APP_CONSTRAINT_RELATIONS.items()
                    )
                    if name in wanted
                ]
            return [{"conname": name} for name in wanted & REQUIRED_AUDIT_CONSTRAINTS]
        if "FROM pg_trigger" in sql:
            if self.app:
                relations = dict(REQUIRED_APP_TRIGGER_RELATIONS)
                if self.slice1:
                    relations.update(REQUIRED_SLICE1_APP_TRIGGER_RELATIONS)
                if self.slice1_runtime:
                    relations.update(REQUIRED_SLICE1_RUNTIME_APP_TRIGGER_RELATIONS)
                return [
                    {
                        "tgname": name,
                        "enabled": "O",
                        "relname": relations[name],
                    }
                    for name in wanted & set(relations)
                    if name in relations
                ]
            return [
                {
                    "tgname": "usage_events_rollup_dirty_days",
                    "enabled": "O",
                    "relname": "usage_events",
                },
                {
                    "tgname": "usage_events_append_only_v2",
                    "enabled": self.append_mode,
                    "relname": "usage_events",
                },
            ]
        raise AssertionError(f"unexpected capability fetch: {sql}")

    async def fetchval(self, sql, *_params):
        if "infra_metering_control" in sql:
            return True
        if "to_regprocedure" in sql or "WITH wanted AS" in sql:
            return True
        raise AssertionError(f"unexpected capability fetchval: {sql}")


@pytest.mark.asyncio
async def test_capability_probe_requires_normal_write_triggers_and_seed_rows():
    app = _CapabilityPool(app=True)
    disabled_append = _CapabilityPool(app=False, append_mode="D")
    capabilities = await probe_schema_capabilities(app, disabled_append)  # type: ignore[arg-type]
    assert not capabilities.append_only_trigger
    assert not capabilities.v2_reads_ready
    assert not capabilities.slice0_ready

    capabilities = await probe_schema_capabilities(
        app,
        _CapabilityPool(app=False, append_mode="A"),  # type: ignore[arg-type]
    )
    assert capabilities.append_only_trigger
    assert capabilities.app_seed_rows_ready
    assert capabilities.slice0_ready
    assert not capabilities.slice1_inventory_ready

    capabilities = await probe_schema_capabilities(
        _CapabilityPool(app=True, slice1=True),  # type: ignore[arg-type]
        None,
    )
    assert capabilities.slice1_inventory_ready
    assert not capabilities.slice0_ready


@pytest.mark.asyncio
async def test_capability_probe_separates_inventory_from_slice1_runtime():
    audit = _CapabilityPool(app=False, append_mode="A")
    inventory_only = await probe_schema_capabilities(
        _CapabilityPool(app=True, slice1=True),  # type: ignore[arg-type]
        audit,  # type: ignore[arg-type]
    )
    assert inventory_only.slice1_inventory_ready
    assert not inventory_only.slice1_runtime_ready
    assert "infra_usage_day_state.coverage_sequence" in (
        inventory_only.missing_slice1_runtime_app_columns
    )

    complete = await probe_schema_capabilities(
        _CapabilityPool(  # type: ignore[arg-type]
            app=True,
            slice1=True,
            slice1_runtime=True,
        ),
        audit,  # type: ignore[arg-type]
    )
    assert complete.slice1_runtime_ready


@pytest.mark.parametrize(
    "index_name",
    [
        "resource_publication_plans_period_idx",
        "resource_intervals_overlap_idx",
    ],
)
@pytest.mark.asyncio
async def test_capability_probe_rejects_unusable_concurrent_index(index_name):
    capabilities = await probe_schema_capabilities(
        _CapabilityPool(
            app=True,
            slice1=True,
            unusable_indexes={index_name},
        ),  # type: ignore[arg-type]
        None,
    )

    assert index_name in capabilities.missing_app_indexes
    assert not capabilities.slice1_inventory_ready


@pytest.mark.parametrize(
    "index_name",
    [
        "resource_inventory_snapshots_complete_received_idx",
        "resource_inventory_watch_events_invalid_received_idx",
    ],
)
@pytest.mark.asyncio
async def test_capability_probe_rejects_unusable_slice1_sealing_index(index_name):
    capabilities = await probe_schema_capabilities(
        _CapabilityPool(
            app=True,
            slice1=True,
            unusable_indexes={index_name},
        ),  # type: ignore[arg-type]
        None,
    )

    assert index_name in capabilities.missing_slice1_app_indexes
    assert not capabilities.slice1_inventory_ready


@pytest.mark.asyncio
async def test_capability_probe_rejects_expected_index_on_wrong_relation():
    index_name = "resource_intervals_overlap_idx"
    capabilities = await probe_schema_capabilities(
        _CapabilityPool(
            app=True,
            slice1=True,
            wrong_relation_indexes={index_name},
        ),  # type: ignore[arg-type]
        None,
    )

    assert index_name in capabilities.missing_app_indexes
    assert not capabilities.slice1_inventory_ready


def test_settings_are_off_by_default_and_publication_fails_closed():
    assert InfrastructureMeteringSettings.from_env({}) == (
        InfrastructureMeteringSettings()
    )

    with pytest.raises(ValueError, match="requires collector, shadow"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_PUBLICATION_ENABLED": "true"}
        )

    with pytest.raises(ValueError, match="shadow mode requires"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_SHADOW_ENABLED": "yes"}
        )


def test_settings_accept_only_explicit_boolean_values():
    with pytest.raises(ValueError, match="must be a boolean"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_V2_READS_ENABLED": "sometimes"}
        )

    settings = InfrastructureMeteringSettings.from_env(
        {
            "INFRASTRUCTURE_METERING_COLLECTOR_ENABLED": "1",
            "INFRASTRUCTURE_METERING_SHADOW_ENABLED": "true",
            "INFRASTRUCTURE_METERING_PUBLICATION_ENABLED": "yes",
            "INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID": " dev-cluster ",
            "INFRASTRUCTURE_METERING_NAMESPACE_ALLOWLIST": "srw",
        }
    )
    assert settings.publication_enabled
    assert settings.stable_cluster_id == "dev-cluster"

    with pytest.raises(ValueError, match="stable cluster id"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID": "not a cluster/id"}
        )


def test_cutover_and_source_aware_read_gates_are_independent_and_fail_closed():
    with pytest.raises(ValueError, match="source-aware reads require v2 reads"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_SOURCE_AWARE_READS_ENABLED": "true"}
        )

    with pytest.raises(ValueError, match="cutover requires collector, shadow"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_CUTOVER_ENABLED": "true"}
        )

    settings = InfrastructureMeteringSettings.from_env(
        {
            "INFRASTRUCTURE_METERING_V2_READS_ENABLED": "true",
            "INFRASTRUCTURE_METERING_SOURCE_AWARE_READS_ENABLED": "true",
            "INFRASTRUCTURE_METERING_COLLECTOR_ENABLED": "true",
            "INFRASTRUCTURE_METERING_SHADOW_ENABLED": "true",
            "INFRASTRUCTURE_METERING_CUTOVER_ENABLED": "true",
            "INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID": "dev-cluster",
            "INFRASTRUCTURE_METERING_NAMESPACE_ALLOWLIST": "srw",
        }
    )
    assert settings.source_aware_reads_enabled is True
    assert settings.cutover_enabled is True
    assert settings.publication_enabled is False


def test_slice2_storage_gates_are_independent_and_fail_closed():
    defaults = InfrastructureMeteringSettings.from_env({})
    assert defaults.pvc_inventory_enabled is False
    assert defaults.pv_inventory_enabled is False
    assert defaults.pvc_shadow_enabled is False
    assert defaults.pv_shadow_enabled is False
    assert defaults.pvc_publication_enabled is False
    assert defaults.pv_publication_enabled is False
    assert defaults.volume_identity_key_version == ""

    with pytest.raises(ValueError, match="PVC inventory requires its collector"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_PVC_INVENTORY_ENABLED": "true"}
        )

    collector_env = {
        "INFRASTRUCTURE_METERING_COLLECTOR_ENABLED": "true",
        "INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID": "dev-cluster",
        "INFRASTRUCTURE_METERING_NAMESPACE_ALLOWLIST": "srw",
    }
    with pytest.raises(ValueError, match="requires a volume identity key version"):
        InfrastructureMeteringSettings.from_env(
            {
                **collector_env,
                "INFRASTRUCTURE_METERING_PV_INVENTORY_ENABLED": "true",
            }
        )
    with pytest.raises(ValueError, match="volume identity key version must be"):
        InfrastructureMeteringSettings.from_env(
            {
                "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY_VERSION": "bad/key",
            }
        )
    with pytest.raises(ValueError, match="PVC shadow mode requires global shadow"):
        InfrastructureMeteringSettings.from_env(
            {
                **collector_env,
                "INFRASTRUCTURE_METERING_PVC_INVENTORY_ENABLED": "true",
                "INFRASTRUCTURE_METERING_PVC_SHADOW_ENABLED": "true",
            }
        )
    with pytest.raises(ValueError, match="PVC publication requires global publication"):
        InfrastructureMeteringSettings.from_env(
            {
                **collector_env,
                "INFRASTRUCTURE_METERING_SHADOW_ENABLED": "true",
                "INFRASTRUCTURE_METERING_PVC_INVENTORY_ENABLED": "true",
                "INFRASTRUCTURE_METERING_PVC_SHADOW_ENABLED": "true",
                "INFRASTRUCTURE_METERING_PVC_PUBLICATION_ENABLED": "true",
            }
        )

    settings = InfrastructureMeteringSettings.from_env(
        {
            **collector_env,
            "INFRASTRUCTURE_METERING_SHADOW_ENABLED": "true",
            "INFRASTRUCTURE_METERING_PUBLICATION_ENABLED": "true",
            "INFRASTRUCTURE_METERING_PVC_INVENTORY_ENABLED": "true",
            "INFRASTRUCTURE_METERING_PV_INVENTORY_ENABLED": "true",
            "INFRASTRUCTURE_METERING_PVC_SHADOW_ENABLED": "true",
            "INFRASTRUCTURE_METERING_PV_SHADOW_ENABLED": "true",
            "INFRASTRUCTURE_METERING_PVC_PUBLICATION_ENABLED": "true",
            "INFRASTRUCTURE_METERING_PV_PUBLICATION_ENABLED": "true",
            "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY_VERSION": "storage-v1",
            "INFRASTRUCTURE_METERING_VOLUME_RESOURCE_MAPPINGS_JSON": (
                '[{"mappingVersion":"longhorn-v1",'
                '"storageClass":"longhorn-ephemeral",'
                '"csiDriver":"driver.longhorn.io",'
                '"volumeMode":"filesystem",'
                '"resource":"block_volume_longhorn_ephemeral"}]'
            ),
        }
    )
    assert settings.pvc_publication_enabled is True
    assert settings.pv_publication_enabled is True
    assert settings.volume_identity_key_version == "storage-v1"
    assert settings.volume_resource_mappings[0].resource == (
        "block_volume_longhorn_ephemeral"
    )

    with pytest.raises(ValueError, match="contain exactly"):
        InfrastructureMeteringSettings.from_env(
            {
                **collector_env,
                "INFRASTRUCTURE_METERING_VOLUME_RESOURCE_MAPPINGS_JSON": (
                    '[{"mappingVersion":"v1","storageClass":"standard",'
                    '"csiDriver":"","volumeMode":"filesystem",'
                    '"resource":"block_volume_local","wildcard":"no"}]'
                ),
            }
        )


def test_vm_storage_dark_gates_are_independent_and_cannot_activate(monkeypatch):
    import main as orchestrator_main

    defaults = InfrastructureMeteringSettings.from_env({})
    assert defaults.vm_pvc_inventory_enabled is False
    assert defaults.vm_pv_inventory_enabled is False
    assert defaults.vm_pvc_shadow_enabled is False
    assert defaults.vm_pv_shadow_enabled is False
    assert defaults.vm_pvc_publication_enabled is False
    assert defaults.vm_pv_publication_enabled is False
    assert defaults.vm_pv_cluster_wide_rbac_acknowledged is False

    base = {
        "INFRASTRUCTURE_METERING_COLLECTOR_ENABLED": "true",
        "INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID": "dev-cluster",
        "INFRASTRUCTURE_METERING_NAMESPACE_ALLOWLIST": "srw",
        "INFRASTRUCTURE_METERING_VM_STABLE_CLUSTER_ID": "vm-cluster",
        "INFRASTRUCTURE_METERING_VM_NAMESPACE": "agent-vms",
    }
    with pytest.raises(ValueError, match="cluster-wide PV RBAC acknowledgement"):
        InfrastructureMeteringSettings.from_env(
            {
                **base,
                "INFRASTRUCTURE_METERING_VM_PV_INVENTORY_ENABLED": "true",
                "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY_VERSION": "storage-v1",
            }
        )
    with pytest.raises(ValueError, match="volume identity key version"):
        InfrastructureMeteringSettings.from_env(
            {
                **base,
                "INFRASTRUCTURE_METERING_VM_PV_INVENTORY_ENABLED": "true",
                "INFRASTRUCTURE_METERING_VM_PV_CLUSTER_WIDE_RBAC_ACKNOWLEDGED": "true",
            }
        )
    with pytest.raises(ValueError, match="VM PVC shadow mode requires global shadow"):
        InfrastructureMeteringSettings.from_env(
            {
                **base,
                "INFRASTRUCTURE_METERING_VM_PVC_INVENTORY_ENABLED": "true",
                "INFRASTRUCTURE_METERING_VM_PVC_SHADOW_ENABLED": "true",
            }
        )
    with pytest.raises(ValueError, match="VM PVC publication requires"):
        InfrastructureMeteringSettings.from_env(
            {
                **base,
                "INFRASTRUCTURE_METERING_SHADOW_ENABLED": "true",
                "INFRASTRUCTURE_METERING_VM_PVC_INVENTORY_ENABLED": "true",
                "INFRASTRUCTURE_METERING_VM_PVC_SHADOW_ENABLED": "true",
                "INFRASTRUCTURE_METERING_VM_PVC_PUBLICATION_ENABLED": "true",
            }
        )

    settings = InfrastructureMeteringSettings.from_env(
        {
            **base,
            "INFRASTRUCTURE_METERING_SHADOW_ENABLED": "true",
            "INFRASTRUCTURE_METERING_VM_PVC_INVENTORY_ENABLED": "true",
            "INFRASTRUCTURE_METERING_VM_PV_INVENTORY_ENABLED": "true",
            "INFRASTRUCTURE_METERING_VM_PVC_SHADOW_ENABLED": "true",
            "INFRASTRUCTURE_METERING_VM_PV_SHADOW_ENABLED": "true",
            "INFRASTRUCTURE_METERING_VM_PV_CLUSTER_WIDE_RBAC_ACKNOWLEDGED": "true",
            "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY_VERSION": "storage-v1",
        }
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_metering_settings", settings)

    # The existing activation API intentionally remains local-cluster-only.
    # Remote dark gates cannot make either measurement basis schedulable.
    assert (
        orchestrator_main._storage_basis_inventory_enabled("claim-requested") is False
    )
    assert (
        orchestrator_main._storage_basis_inventory_enabled("volume-provisioned")
        is False
    )
    assert settings.pvc_publication_enabled is False
    assert settings.pv_publication_enabled is False


def test_slice3_compute_gates_are_independent_and_fail_closed():
    defaults = InfrastructureMeteringSettings.from_env({})
    assert defaults.ide_pod_shadow_enabled is False
    assert defaults.agent_pod_shadow_enabled is False
    assert defaults.ide_pod_publication_enabled is False
    assert defaults.agent_pod_publication_enabled is False
    assert defaults.vm_inventory_enabled is False
    assert defaults.vm_shadow_enabled is False
    assert defaults.vm_publication_enabled is False

    collector_env = {
        "INFRASTRUCTURE_METERING_COLLECTOR_ENABLED": "true",
        "INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID": "dev-cluster",
        "INFRASTRUCTURE_METERING_NAMESPACE_ALLOWLIST": "srw",
    }
    with pytest.raises(ValueError, match="agent Pod shadow mode requires"):
        InfrastructureMeteringSettings.from_env(
            {
                **collector_env,
                "INFRASTRUCTURE_METERING_AGENT_POD_SHADOW_ENABLED": "true",
            }
        )
    with pytest.raises(ValueError, match="IDE Pod publication requires"):
        InfrastructureMeteringSettings.from_env(
            {
                **collector_env,
                "INFRASTRUCTURE_METERING_SHADOW_ENABLED": "true",
                "INFRASTRUCTURE_METERING_IDE_POD_SHADOW_ENABLED": "true",
                "INFRASTRUCTURE_METERING_IDE_POD_PUBLICATION_ENABLED": "true",
            }
        )
    with pytest.raises(ValueError, match="VM inventory requires VM stable"):
        InfrastructureMeteringSettings.from_env(
            {
                **collector_env,
                "INFRASTRUCTURE_METERING_VM_INVENTORY_ENABLED": "true",
            }
        )
    with pytest.raises(ValueError, match="VM namespace must be"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_VM_NAMESPACE": "Not_Valid"}
        )

    settings = InfrastructureMeteringSettings.from_env(
        {
            **collector_env,
            "INFRASTRUCTURE_METERING_SHADOW_ENABLED": "true",
            "INFRASTRUCTURE_METERING_PUBLICATION_ENABLED": "true",
            "INFRASTRUCTURE_METERING_IDE_POD_SHADOW_ENABLED": "true",
            "INFRASTRUCTURE_METERING_AGENT_POD_SHADOW_ENABLED": "true",
            "INFRASTRUCTURE_METERING_IDE_POD_PUBLICATION_ENABLED": "true",
            "INFRASTRUCTURE_METERING_AGENT_POD_PUBLICATION_ENABLED": "true",
            "INFRASTRUCTURE_METERING_VM_INVENTORY_ENABLED": "true",
            "INFRASTRUCTURE_METERING_VM_SHADOW_ENABLED": "true",
            "INFRASTRUCTURE_METERING_VM_PUBLICATION_ENABLED": "true",
            "INFRASTRUCTURE_METERING_VM_STABLE_CLUSTER_ID": "vm-dev",
            "INFRASTRUCTURE_METERING_VM_NAMESPACE": "srw-vms",
        }
    )
    assert settings.agent_pod_publication_enabled is True
    assert settings.ide_pod_publication_enabled is True
    assert settings.vm_publication_enabled is True
    assert settings.vm_stable_cluster_id == "vm-dev"
    assert settings.vm_namespace == "srw-vms"


def test_slice1_collector_settings_are_bounded_and_fail_closed():
    settings = InfrastructureMeteringSettings.from_env(
        {
            "INFRASTRUCTURE_METERING_COLLECTOR_ENABLED": "true",
            "INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID": "dev-cluster",
            "INFRASTRUCTURE_METERING_NAMESPACE_ALLOWLIST": "srw,agents,srw",
            "INFRASTRUCTURE_METERING_LIST_PAGE_SIZE": "250",
            "INFRASTRUCTURE_METERING_MAX_SNAPSHOT_ITEMS": "25000",
            "INFRASTRUCTURE_METERING_SNAPSHOT_ITEM_RETENTION_DAYS": "14",
            "INFRASTRUCTURE_METERING_DIAGNOSTIC_RETENTION_DAYS": "42",
            "INFRASTRUCTURE_METERING_CLEANUP_INTERVAL_SECONDS": "120",
            "INFRASTRUCTURE_METERING_MAX_COLLECTOR_CLOCK_SKEW_SECONDS": "45",
        }
    )
    assert settings.namespace_allowlist == ("srw", "agents")
    assert settings.list_page_size == 250
    assert settings.max_snapshot_items == 25_000
    assert settings.snapshot_item_retention_days == 14
    assert settings.diagnostic_retention_days == 42
    assert settings.cleanup_interval_seconds == 120
    assert settings.max_collector_clock_skew_seconds == 45

    with pytest.raises(ValueError, match="stable cluster id"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_COLLECTOR_ENABLED": "true"}
        )
    with pytest.raises(ValueError, match="at least one namespace"):
        InfrastructureMeteringSettings.from_env(
            {
                "INFRASTRUCTURE_METERING_COLLECTOR_ENABLED": "true",
                "INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID": "dev-cluster",
            }
        )
    with pytest.raises(ValueError, match="in-process collection is not implemented"):
        InfrastructureMeteringSettings.from_env(
            {
                "INFRASTRUCTURE_METERING_COLLECTOR_ENABLED": "true",
                "INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID": "dev-cluster",
                "INFRASTRUCTURE_METERING_NAMESPACE_ALLOWLIST": "srw",
                "INFRASTRUCTURE_METERING_DEPLOYMENT_MODE": "in-process",
            }
        )
    with pytest.raises(ValueError, match="invalid Kubernetes namespaces"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_NAMESPACE_ALLOWLIST": "srw,Not_Valid"}
        )
    with pytest.raises(ValueError, match="between 15 and 86400"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_RELIST_INTERVAL_SECONDS": "1"}
        )
    with pytest.raises(ValueError, match="between 1 and 3600"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_MAX_COLLECTOR_CLOCK_SKEW_SECONDS": "3601"}
        )
    with pytest.raises(ValueError, match="stale-after"):
        InfrastructureMeteringSettings.from_env(
            {
                "INFRASTRUCTURE_METERING_RELIST_INTERVAL_SECONDS": "600",
                "INFRASTRUCTURE_METERING_STALE_AFTER_SECONDS": "300",
            }
        )
    with pytest.raises(ValueError, match="diagnostic retention"):
        InfrastructureMeteringSettings.from_env(
            {
                "INFRASTRUCTURE_METERING_SNAPSHOT_ITEM_RETENTION_DAYS": "30",
                "INFRASTRUCTURE_METERING_DIAGNOSTIC_RETENTION_DAYS": "14",
            }
        )


def test_decimal_wire_values_are_exact_and_non_exponent():
    assert decimal_text(Decimal("12.500000")) == "12.5"
    assert decimal_text(Decimal("1E-18")) == "0.000000000000000001"
    assert decimal_text(Decimal("-0.000")) == "0"
    assert decimal_text(Decimal("0.0000000000000000005")) == "0"
    assert decimal_text(Decimal("1.0000000000000000015")) == "1.000000000000000002"
    assert (
        decimal_text(Decimal("99999999999999999999.999999999999999999"))
        == "99999999999999999999.999999999999999999"
    )

    with pytest.raises(ValueError, match=r"NUMERIC\(38,18\)"):
        decimal_text(Decimal("100000000000000000000"))


@pytest.mark.parametrize("value", [0.1, True, "", "NaN", "Infinity"])
def test_decimal_wire_values_reject_inexact_or_non_finite_inputs(value):
    with pytest.raises(ValueError):
        decimal_text(value)


def test_cost_coverage_distinguishes_free_unpriced_and_partial():
    free = ledger_cost(amount=0, priced_quantity="4", unpriced_quantity="0")
    assert free.status == "priced"
    assert free.amount == "0"

    unpriced = ledger_cost(amount=None, priced_quantity="0", unpriced_quantity="4")
    assert unpriced.status == "unpriced"
    assert unpriced.amount is None

    partial = ledger_cost(amount="1.25", priced_quantity="2", unpriced_quantity="2")
    assert partial.status == "partially-priced"
    assert partial.amount == "1.25"

    zero_quantity_unpriced = ledger_cost(
        amount=None,
        priced_quantity="0",
        unpriced_quantity="0",
        priced_events=0,
        unpriced_events=1,
    )
    assert zero_quantity_unpriced.status == "unpriced"

    corrected = ledger_cost(
        amount="1",
        priced_quantity="4",
        unpriced_quantity="0",
        priced_events=1,
        unpriced_events=2,
    )
    assert corrected.status == "priced"

    empty = ledger_cost(amount=None, priced_quantity="0", unpriced_quantity="0")
    assert empty.status == "priced"
    assert empty.amount == "0"

    with pytest.raises(ValidationError, match="requires an amount"):
        ledger_cost(amount=None, priced_quantity="1", unpriced_quantity="0")


def test_typed_contracts_reject_impossible_cross_field_states():
    with pytest.raises(ValidationError, match="requires an amount"):
        UsageLedgerCostV2(
            status="priced",
            amount=None,
            priced_quantity="1",
            unpriced_quantity="0",
        )

    with pytest.raises(ValidationError, match="ledger quantity buckets"):
        UsageRowV2(
            category="compute",
            measurement_basis="scheduler-request",
            cost_domain="workload-allocation",
            resource_class="kubernetes-pod",
            measurement_algorithm="fixture-v1",
            resource="workspace_pod",
            unit="vcpu-hour",
            attribution_scope="customer",
            quantity="2",
            finalized_quantity="1",
            confirmed_provisional_quantity="1",
            ledger_cost=ledger_cost(
                amount=None, priced_quantity="0", unpriced_quantity="1"
            ),
            events=1,
        )

    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    with pytest.raises(ValidationError, match="end must be after start"):
        UsageWindowV2(start=now, end=now, as_of=now, data_through=None)
    with pytest.raises(ValidationError, match="cannot exceed"):
        UsageCoverageV2(
            status="partial",
            includes_provisional=False,
            required_sources_ok=2,
            required_sources_total=1,
        )


def test_query_adapter_derives_total_from_quantized_pricing_buckets():
    row = UsageV2QueryService._row(
        {
            "category": "compute",
            "measurement_basis": "scheduler-request",
            "cost_domain": "workload-allocation",
            "resource_class": "kubernetes-pod",
            "measurement_algorithm": "legacy-end-stamped-v1",
            "resource": "workspace_pod",
            "unit": "vcpu-hour",
            "attribution_scope": "customer",
            "quantity": Decimal("0.000000000000000001"),
            "cost_usd": Decimal("0"),
            "priced_quantity": Decimal("0.0000000000000000005"),
            "unpriced_quantity": Decimal("0.0000000000000000005"),
            "priced_events": 1,
            "unpriced_events": 1,
            "events": 2,
        }
    )

    assert row.quantity == "0"
    assert row.ledger_cost.priced_quantity == "0"
    assert row.ledger_cost.unpriced_quantity == "0"


def test_usage_row_rejects_an_unknown_typed_dimension():
    with pytest.raises(ValidationError):
        UsageRowV2(
            category="compute",
            measurement_basis="made-up",
            cost_domain="workload-allocation",
            resource_class="kubernetes-pod",
            measurement_algorithm="fixture-v1",
            resource="workspace_pod",
            unit="vcpu-hour",
            attribution_scope="customer",
            quantity="1",
            finalized_quantity="1",
            confirmed_provisional_quantity="0",
            ledger_cost=ledger_cost(
                amount=None, priced_quantity="0", unpriced_quantity="1"
            ),
            events=1,
        )


class _AuditPool:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, sql, *params):
        self.calls.append((sql, params))
        return self.rows


class _LedgerPool:
    def __init__(self):
        self.calls = []

    def acquire(self):
        pool = self

        class _Acquire:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def fetch(self, sql, *params):
                pool.calls.append((sql, params))
                return []

        return _Acquire()


@pytest.mark.asyncio
async def test_v2_summary_keeps_cpu_and_memory_as_separate_decimal_rows():
    audit = _AuditPool(
        [
            {
                "category": "compute",
                "measurement_basis": "scheduler-request",
                "cost_domain": "workload-allocation",
                "resource_class": "kubernetes-pod",
                "measurement_algorithm": "legacy-end-stamped-v1",
                "resource": "workspace_pod",
                "unit": "vcpu-hour",
                "attribution_scope": "customer",
                "quantity": Decimal("8.000000"),
                "cost_usd": None,
                "priced_quantity": Decimal("0"),
                "unpriced_quantity": Decimal("8"),
                "priced_events": 0,
                "unpriced_events": 1,
                "events": 1,
            },
            {
                "category": "compute",
                "measurement_basis": "scheduler-request",
                "cost_domain": "workload-allocation",
                "resource_class": "kubernetes-pod",
                "measurement_algorithm": "legacy-end-stamped-v1",
                "resource": "workspace_pod",
                "unit": "gib-hour",
                "attribution_scope": "customer",
                "quantity": Decimal("16.000000"),
                "cost_usd": Decimal("0"),
                "priced_quantity": Decimal("16"),
                "unpriced_quantity": Decimal("0"),
                "priced_events": 1,
                "unpriced_events": 0,
                "events": 1,
            },
        ]
    )
    service = UsageV2QueryService(audit, _read_capabilities())
    start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    end = datetime(2026, 8, 6, tzinfo=timezone.utc)
    user_id = str(uuid4())

    result = await service.summary(
        from_ts=start,
        to_ts=end,
        visibility=UsageVisibility(owner_user_id=user_id),
        as_of=end,
    )

    assert [(row.unit, row.quantity) for row in result.rows] == [
        ("vcpu-hour", "8"),
        ("gib-hour", "16"),
    ]
    assert result.rows[0].ledger_cost.status == "unpriced"
    assert result.rows[1].ledger_cost.status == "priced"
    assert result.rows[1].ledger_cost.amount == "0"
    assert result.coverage.status == "partial"
    assert "live-resource-inventory" in result.coverage.excluded_domains

    sql, params = audit.calls[0]
    assert "period_start IS NULL AND ts >= $1 AND ts < $2" in sql
    assert "EXTRACT(EPOCH" not in sql
    assert "attribution_scope = 'customer'" in sql
    assert params[:3] == (start, end, False)
    assert params[3].hex == user_id.replace("-", "")
    assert result.window.data_through is None
    assert "typed-infrastructure-intervals" in result.coverage.excluded_domains


@pytest.mark.asyncio
async def test_v2_project_scope_narrows_the_identity_visibility_union():
    audit = _AuditPool([])
    service = UsageV2QueryService(
        audit,
        _read_capabilities(),
    )
    start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    end = datetime(2026, 8, 6, tzinfo=timezone.utc)
    owner_id = str(uuid4())
    visible_project_id = str(uuid4())
    scope_project_id = str(uuid4())

    await service.summary(
        from_ts=start,
        to_ts=end,
        visibility=UsageVisibility(
            owner_user_id=owner_id,
            visible_project_ids=(visible_project_id,),
            scope_project_id=scope_project_id,
        ),
        as_of=end,
    )

    sql, params = audit.calls[0]
    assert "user_id = $4" in sql
    assert "project_id = ANY($5::uuid[])" in sql
    assert "project_id = $6" in sql
    assert params[3] == UUID(owner_id)
    assert params[4] == [UUID(visible_project_id)]
    assert params[5] == UUID(scope_project_id)


@pytest.mark.asyncio
async def test_v1_queries_are_frozen_to_llm_and_workspace_cpu_memory():
    pool = _LedgerPool()
    ledger = UsageLedger(pool, UsageRates(None))
    start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    end = datetime(2026, 8, 6, tzinfo=timezone.utc)

    await ledger.query_usage(from_ts=start, to_ts=end)
    await ledger.query_grouped(from_ts=start, to_ts=end, group_by="model")

    usage_sql = pool.calls[0][0]
    model_sql = pool.calls[1][0]
    assert "category IN ('llm', 'tts', 'stt')" in usage_sql
    assert "resource = 'workspace_pod'" in usage_sql
    assert "unit IN ('vcpu-hour', 'gib-hour')" in usage_sql
    assert "category = 'llm'" in model_sql


@pytest.mark.asyncio
async def test_v1_project_scope_narrows_summary_and_strict_self_views():
    pool = _LedgerPool()
    ledger = UsageLedger(pool, UsageRates(None))
    start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    end = datetime(2026, 8, 6, tzinfo=timezone.utc)
    owner_id = str(uuid4())
    visible_project_id = str(uuid4())
    scope_project_id = str(uuid4())

    await ledger.query_usage(
        from_ts=start,
        to_ts=end,
        owner_user_id=owner_id,
        visible_project_ids=(visible_project_id,),
        scope_project_id=scope_project_id,
    )
    await ledger.query_grouped(
        from_ts=start,
        to_ts=end,
        group_by="user",
        owner_user_id=owner_id,
        scope_project_id=scope_project_id,
    )

    summary_sql, summary_params = pool.calls[0]
    breakdown_sql, breakdown_params = pool.calls[1]
    assert "user_id = $3" in summary_sql
    assert "project_id = ANY($4::uuid[])" in summary_sql
    assert "project_id = $5" in summary_sql
    assert summary_params[2] == UUID(owner_id)
    assert summary_params[3] == [UUID(visible_project_id)]
    assert summary_params[4] == UUID(scope_project_id)
    assert "user_id = $3" in breakdown_sql
    assert "project_id = $4" in breakdown_sql
    assert breakdown_params[2] == UUID(owner_id)
    assert breakdown_params[3] == UUID(scope_project_id)


@pytest.mark.asyncio
async def test_v2_service_refuses_reads_without_audit_capability():
    service = UsageV2QueryService(_AuditPool([]), MeteringSchemaCapabilities())
    now = datetime.now(timezone.utc)
    with pytest.raises(RuntimeError, match="schema is unavailable"):
        await service.summary(
            from_ts=now,
            to_ts=now.replace(year=now.year + 1),
            visibility=UsageVisibility(),
        )


def test_source_aware_service_requires_both_slice0_and_slice1_readiness():
    audit = _AuditPool([])
    app = MagicMock()
    slice0_only = UsageV2QueryService(
        audit,
        _source_aware_capabilities(slice1=False),
        app,
        source_aware_reads_enabled=True,
    )
    ready = UsageV2QueryService(
        audit,
        _source_aware_capabilities(slice1=True),
        app,
        source_aware_reads_enabled=True,
    )
    missing_app_pool = UsageV2QueryService(
        audit,
        _source_aware_capabilities(slice1=True),
        source_aware_reads_enabled=True,
    )

    assert slice0_only.source_aware_reads_enabled is True
    assert slice0_only.is_available is False
    assert ready.is_available is True
    assert missing_app_pool.is_available is False


def test_legacy_v2_readiness_is_unchanged_while_source_aware_gate_is_off():
    service = UsageV2QueryService(
        _AuditPool([]),
        _read_capabilities(),
        source_aware_reads_enabled=False,
    )

    assert service.source_aware_reads_enabled is False
    assert service.is_available is True


@pytest.mark.asyncio
async def test_usage_v2_route_is_hidden_while_its_gate_is_off(monkeypatch):
    import main as orchestrator_main

    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        orchestrator_main.InfrastructureMeteringSettings(),
    )
    auth = AsyncMock()
    monkeypatch.setattr(orchestrator_main, "require_approved_user", auth)

    with pytest.raises(HTTPException) as raised:
        await orchestrator_main.get_usage_v2(
            MagicMock(),
            days=30,
            from_date=None,
            to_date=None,
            ref_id=None,
            include_non_customer=False,
        )

    assert raised.value.status_code == 404
    auth.assert_not_awaited()


@pytest.mark.asyncio
async def test_usage_v2_restricts_non_customer_rows_to_fleet_admin(monkeypatch):
    import main as orchestrator_main

    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        orchestrator_main.InfrastructureMeteringSettings(v2_reads_enabled=True),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "require_approved_user",
        AsyncMock(return_value={"id": str(uuid4()), "is_admin": False, "scopes": []}),
    )

    with pytest.raises(HTTPException) as raised:
        await orchestrator_main.get_usage_v2(
            MagicMock(),
            days=30,
            from_date=None,
            to_date=None,
            ref_id=None,
            include_non_customer=True,
        )

    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_usage_v2_fleet_admin_passes_explicit_visibility(monkeypatch):
    import main as orchestrator_main

    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    response = UsageV2QueryService._row(
        {
            "category": "compute",
            "measurement_basis": "scheduler-request",
            "cost_domain": "workload-allocation",
            "resource_class": "kubernetes-pod",
            "measurement_algorithm": "fixture-v1",
            "resource": "workspace_pod",
            "unit": "vcpu-hour",
            "attribution_scope": "customer",
            "quantity": Decimal("1"),
            "cost_usd": None,
            "priced_quantity": Decimal("0"),
            "unpriced_quantity": Decimal("1"),
            "priced_events": 0,
            "unpriced_events": 1,
            "events": 1,
        }
    )
    summary = {
        "schema_version": 2,
        "window": {
            "start": now,
            "end": now.replace(day=7),
            "as_of": now,
            "data_through": now,
        },
        "rows": [response],
        "coverage": {
            "status": "partial",
            "includes_provisional": False,
            "required_sources_ok": 0,
            "required_sources_total": 0,
            "unknown_ranges": [],
            "excluded_domains": [],
        },
    }

    class _Service:
        is_available = True

        def __init__(self):
            self.kwargs = None

        async def summary(self, **kwargs):
            self.kwargs = kwargs
            return summary

    service = _Service()

    class _Rollup:
        async def bootstrap_state(self):
            return MagicMock(read_ready=True)

    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        orchestrator_main.InfrastructureMeteringSettings(v2_reads_enabled=True),
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_usage_v2", service)
    monkeypatch.setattr(orchestrator_main, "infrastructure_usage_rollup", _Rollup())
    monkeypatch.setattr(
        orchestrator_main,
        "require_approved_user",
        AsyncMock(return_value={"id": str(uuid4()), "is_admin": True, "scopes": []}),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "_visibility_kwargs_for_stats",
        AsyncMock(return_value={}),
    )

    result = await orchestrator_main.get_usage_v2(
        MagicMock(),
        days=1,
        from_date="2026-08-05T00:00:00Z",
        to_date="2026-08-06T00:00:00Z",
        ref_id=None,
        include_non_customer=True,
    )

    assert result == summary
    assert service.kwargs["visibility"].include_non_customer is True


@pytest.mark.asyncio
async def test_usage_v2_refuses_reads_until_bootstrap_is_complete(monkeypatch):
    import main as orchestrator_main

    class _Service:
        is_available = True

    class _Rollup:
        async def bootstrap_state(self):
            return MagicMock(read_ready=False)

    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        orchestrator_main.InfrastructureMeteringSettings(v2_reads_enabled=True),
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_usage_v2", _Service())
    monkeypatch.setattr(orchestrator_main, "infrastructure_usage_rollup", _Rollup())
    monkeypatch.setattr(
        orchestrator_main,
        "require_approved_user",
        AsyncMock(return_value={"id": str(uuid4()), "is_admin": True, "scopes": []}),
    )

    with pytest.raises(HTTPException) as raised:
        await orchestrator_main.get_usage_v2(
            MagicMock(),
            days=1,
            from_date="2026-08-05T00:00:00Z",
            to_date="2026-08-06T00:00:00Z",
            ref_id=None,
            include_non_customer=False,
        )

    assert raised.value.status_code == 503
    assert "bootstrap incomplete" in raised.value.detail


@pytest.mark.asyncio
async def test_usage_v2_does_not_echo_server_contract_failures_as_client_errors(
    monkeypatch,
):
    import main as orchestrator_main

    class _Service:
        is_available = True

        async def summary(self, **_kwargs):
            raise ValueError("sensitive-invalid-ledger-value")

    class _Rollup:
        async def bootstrap_state(self):
            return MagicMock(read_ready=True)

    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        orchestrator_main.InfrastructureMeteringSettings(v2_reads_enabled=True),
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_usage_v2", _Service())
    monkeypatch.setattr(orchestrator_main, "infrastructure_usage_rollup", _Rollup())
    monkeypatch.setattr(
        orchestrator_main,
        "require_approved_user",
        AsyncMock(return_value={"id": str(uuid4()), "is_admin": True, "scopes": []}),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "_visibility_kwargs_for_stats",
        AsyncMock(return_value={}),
    )

    with pytest.raises(HTTPException) as raised:
        await orchestrator_main.get_usage_v2(
            MagicMock(),
            days=1,
            from_date="2026-08-05T00:00:00Z",
            to_date="2026-08-06T00:00:00Z",
            ref_id=None,
            include_non_customer=False,
        )

    assert raised.value.status_code == 500
    assert raised.value.detail == "Usage API v2 query failed"


def test_internal_inventory_ingestion_routes_are_hidden_from_openapi():
    import main as orchestrator_main

    prefix = "/api/internal/infrastructure-metering/v1"
    expected = {
        f"{prefix}/tickets",
        f"{prefix}/snapshots/begin",
        f"{prefix}/snapshots/items",
        f"{prefix}/snapshots/finalize",
        f"{prefix}/watch/apply",
        f"{prefix}/watch/finish",
    }
    routes = {
        route.path: route
        for route in orchestrator_main.app.routes
        if getattr(route, "path", "") in expected
    }

    assert set(routes) == expected
    assert all(route.methods == {"POST"} for route in routes.values())
    assert all(not route.include_in_schema for route in routes.values())


@pytest.mark.asyncio
async def test_infrastructure_admin_operations_require_real_fleet_view(monkeypatch):
    import main as orchestrator_main

    request = MagicMock()
    request.url.path = "/api/admin/usage/v2/infrastructure-cutover"
    audit = AsyncMock()
    monkeypatch.setattr(orchestrator_main, "log_security_event", audit)
    monkeypatch.setattr(
        orchestrator_main,
        "_require_admin",
        AsyncMock(
            return_value={
                "id": str(uuid4()),
                "real_is_admin": True,
                "is_admin": False,
            }
        ),
    )

    with pytest.raises(HTTPException) as raised:
        await orchestrator_main._require_infrastructure_fleet_admin(request)

    assert raised.value.status_code == 403
    assert audit.await_args.kwargs["event_type"] == "admin_denied"

    monkeypatch.setattr(
        orchestrator_main,
        "_require_admin",
        AsyncMock(
            return_value={
                "id": str(uuid4()),
                "real_is_admin": True,
                "is_admin": True,
            }
        ),
    )
    monkeypatch.setattr(
        orchestrator_main, "mcp_scope_project_id", lambda _user: uuid4()
    )
    with pytest.raises(HTTPException) as scoped:
        await orchestrator_main._require_infrastructure_fleet_admin(request)
    assert scoped.value.status_code == 403
    assert audit.await_count == 2


@pytest.mark.asyncio
async def test_cutover_prepare_is_explicit_gated_idempotent_admin_operation(
    monkeypatch,
):
    import main as orchestrator_main
    from orchestrator.services.infrastructure_metering.cutover import (
        CutoverPhase,
        CutoverStatus,
    )

    actor_id, request_id = uuid4(), uuid4()
    request = MagicMock()
    request.url.path = "/api/admin/usage/v2/infrastructure-cutover/prepare"
    monkeypatch.setattr(
        orchestrator_main,
        "_require_infrastructure_fleet_admin",
        AsyncMock(return_value={"id": str(actor_id), "is_admin": True}),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        InfrastructureMeteringSettings(cutover_enabled=True),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_durable_reporting_policy_ready",
        True,
    )
    monkeypatch.setattr(
        orchestrator_main, "_infrastructure_leader_generation", lambda: 9
    )
    status = CutoverStatus(
        state="preparing",
        phase=CutoverPhase.LEGACY_DRAINING,
        leader_generation=9,
        cutover_at=datetime(2026, 8, 6, 12, tzinfo=timezone.utc),
        request_id=request_id,
        actor_id=actor_id,
        reason="reviewed shadow window",
        unplanned_intervals=1,
        planned=0,
        published=0,
        conflicts=0,
        open_legacy_intervals=0,
        cutover_error=None,
    )
    coordinator = MagicMock()
    coordinator.prepare = AsyncMock(return_value=status)
    monkeypatch.setattr(
        orchestrator_main, "infrastructure_workspace_cutover", coordinator
    )
    audit = AsyncMock()
    monkeypatch.setattr(orchestrator_main, "log_security_event", audit)

    result = await orchestrator_main.prepare_infrastructure_metering_cutover(
        request,
        orchestrator_main.InfrastructureCutoverPrepareRequest(
            idempotency_key=request_id,
            reason="reviewed shadow window",
        ),
    )

    coordinator.prepare.assert_awaited_once_with(
        9,
        actor_id=actor_id,
        reason="reviewed shadow window",
        idempotency_key=request_id,
    )
    assert result["phase"] == "legacy-draining"
    assert result["request_id"] == request_id
    assert audit.await_args.kwargs["event_type"] == (
        "infrastructure_metering_cutover_prepared"
    )


@pytest.mark.asyncio
async def test_coverage_waiver_route_maps_result_and_audits(monkeypatch):
    import main as orchestrator_main
    from orchestrator.services.infrastructure_metering.coverage import (
        CoverageDayDegradation,
        CoverageGapWaiverResult,
    )

    actor_id, gap_id, request_id = uuid4(), uuid4(), uuid4()
    resolved_at = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    request = MagicMock()
    request.url.path = f"/api/admin/usage/v2/coverage-gaps/{gap_id}/waive"
    monkeypatch.setattr(
        orchestrator_main,
        "_require_infrastructure_fleet_admin",
        AsyncMock(return_value={"id": str(actor_id), "is_admin": True}),
    )
    service = MagicMock()
    service.waive = AsyncMock(
        return_value=CoverageGapWaiverResult(
            gap_id=gap_id,
            actor_id=actor_id,
            idempotency_key=request_id,
            reason="durable journal unavailable",
            resolved_at=resolved_at,
            replayed=False,
            degraded_days=(
                CoverageDayDegradation(
                    day=resolved_at.date(),
                    coverage_sequence=2,
                    coverage_revision="waiver-v1:revision",
                    added_range=(resolved_at, resolved_at + timedelta(minutes=5)),
                ),
            ),
        )
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_coverage_waivers", service)
    audit = AsyncMock()
    monkeypatch.setattr(orchestrator_main, "log_security_event", audit)

    result = await orchestrator_main.waive_infrastructure_metering_coverage_gap(
        request,
        gap_id,
        orchestrator_main.InfrastructureCoverageWaiverRequest(
            idempotency_key=request_id,
            reason="durable journal unavailable",
        ),
    )

    service.waive.assert_awaited_once_with(
        gap_id,
        actor_id,
        "durable journal unavailable",
        request_id,
    )
    assert result["degraded_days"][0]["coverage_sequence"] == 2
    assert audit.await_args.kwargs["event_type"] == (
        "infrastructure_metering_coverage_gap_waived"
    )


@pytest.mark.asyncio
async def test_correction_route_is_idempotent_fleet_admin_operation(monkeypatch):
    import main as orchestrator_main

    actor_id, correction_id = uuid4(), uuid4()
    period_start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    period_end = period_start + timedelta(hours=1)
    request = MagicMock()
    request.url.path = "/api/admin/usage/v2/infrastructure-corrections"
    monkeypatch.setattr(
        orchestrator_main,
        "_require_infrastructure_fleet_admin",
        AsyncMock(return_value={"id": str(actor_id), "is_admin": True}),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        InfrastructureMeteringSettings(
            collector_enabled=True,
            shadow_enabled=True,
            publication_enabled=True,
            stable_cluster_id="dev-cluster",
            namespace_allowlist=("srw",),
        ),
    )
    monkeypatch.setattr(
        orchestrator_main, "_infrastructure_leader_generation", lambda: 11
    )
    plan = MagicMock(
        id=correction_id,
        correction_group_id=correction_id,
        state="planned",
        plan_revision=2,
        period_start=period_start,
        period_end=period_end,
        events=(object(), object()),
        event_set_hash="a" * 64,
        rate_selection_hash="b" * 64,
    )
    materializer = MagicMock()
    materializer.create_correction = AsyncMock(return_value=plan)
    monkeypatch.setattr(
        orchestrator_main, "infrastructure_usage_materializer", materializer
    )
    audit = AsyncMock()
    monkeypatch.setattr(orchestrator_main, "log_security_event", audit)
    original_ts = datetime(2026, 8, 5, tzinfo=timezone.utc)

    result = await orchestrator_main.create_infrastructure_metering_correction(
        request,
        orchestrator_main.InfrastructureCorrectionRequest(
            idempotency_key=correction_id,
            reason="reviewed attribution repair",
            deltas=[
                orchestrator_main.InfrastructureCorrectionDeltaRequest(
                    source="infra-allocation-v2",
                    source_id="original-source-id",
                    unit="vcpu-hour",
                    ts=original_ts,
                    expected_payload_hash="c" * 64,
                    quantity=Decimal("-4"),
                ),
                orchestrator_main.InfrastructureCorrectionDeltaRequest(
                    source="infra-allocation-v2",
                    source_id="original-source-id",
                    unit="vcpu-hour",
                    ts=original_ts,
                    expected_payload_hash="c" * 64,
                    quantity=Decimal("4"),
                    payload_overrides={"user_id": str(uuid4())},
                ),
            ],
        ),
    )

    call = materializer.create_correction.await_args
    assert call.args[0] == 11
    assert [delta.quantity for delta in call.args[1]] == [Decimal("-4"), Decimal("4")]
    assert call.kwargs == {
        "correction_reason": "reviewed attribution repair",
        "correction_actor_id": actor_id,
        "correction_id": correction_id,
    }
    assert result["plan_id"] == correction_id
    assert result["event_count"] == 2
    assert audit.await_args.kwargs["event_type"] == (
        "infrastructure_metering_correction_reviewed"
    )


def test_infrastructure_admin_routes_are_explicit_and_publicly_documented():
    import main as orchestrator_main

    expected = {
        "/api/admin/usage/v2/infrastructure-cutover": {"GET"},
        "/api/admin/usage/v2/infrastructure-cutover/prepare": {"POST"},
        "/api/admin/usage/v2/coverage-gaps/{gap_id}/waive": {"POST"},
        "/api/admin/usage/v2/infrastructure-corrections": {"POST"},
        "/api/admin/usage/v2/storage-activation": {"GET"},
        "/api/admin/usage/v2/storage-activation/{measurement_basis}/shadow": {"POST"},
        "/api/admin/usage/v2/storage-activation/{measurement_basis}/schedule": {"POST"},
        (
            "/api/admin/usage/v2/storage-source-activation/"
            "{source}/{measurement_basis}/shadow"
        ): {"POST"},
        (
            "/api/admin/usage/v2/storage-source-activation/"
            "{source}/{measurement_basis}/schedule"
        ): {"POST"},
        "/api/admin/usage/v2/compute-activation": {"GET"},
        "/api/admin/usage/v2/compute-activation/{activation_key}/shadow": {"POST"},
        "/api/admin/usage/v2/compute-activation/{activation_key}/schedule": {"POST"},
        "/api/admin/usage/v2/compute-activation/{activation_key}/rollover": {"POST"},
        "/api/admin/usage/v2/storage-assets/{asset_id}/destroy": {"POST"},
    }
    routes = {
        route.path: route.methods
        for route in orchestrator_main.app.routes
        if getattr(route, "path", "") in expected
    }
    assert routes == expected


@pytest.mark.asyncio
async def test_storage_activation_routes_are_explicit_and_audited(monkeypatch):
    import main as orchestrator_main
    from orchestrator.services.infrastructure_metering.storage_assets import (
        StorageActivation,
        StorageSourceActivation,
    )

    actor_id = uuid4()
    request = MagicMock()
    request.url.path = "/api/admin/usage/v2/storage-activation/claim-requested/shadow"
    monkeypatch.setattr(
        orchestrator_main,
        "_require_infrastructure_fleet_admin",
        AsyncMock(return_value={"id": str(actor_id), "is_admin": True}),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        InfrastructureMeteringSettings(
            collector_enabled=True,
            pvc_inventory_enabled=True,
            stable_cluster_id="dev-cluster",
            namespace_allowlist=("srw",),
        ),
    )
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    store = MagicMock()
    store.enter_source_shadow = AsyncMock(
        return_value=StorageSourceActivation(
            measurement_basis="claim-requested",
            collector_id="kubernetes-pods",
            source_cluster="dev-cluster",
            state="shadow",
            activated_at=None,
            database_time=now,
        )
    )
    store.read_activations = AsyncMock(
        return_value=(
            StorageActivation("claim-requested", "shadow", None, now),
            StorageActivation("volume-provisioned", "disabled", None, now),
        )
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_storage_assets", store)
    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_storage_source_activation_ready",
        True,
    )
    audit = AsyncMock()
    monkeypatch.setattr(orchestrator_main, "log_security_event", audit)

    result = await orchestrator_main.enter_infrastructure_storage_shadow(
        request,
        "claim-requested",
        orchestrator_main.InfrastructureStorageActivationRequest(
            reason="inventory soak is healthy"
        ),
    )

    store.enter_source_shadow.assert_awaited_once_with(
        measurement_basis="claim-requested",
        collector_id="kubernetes-pods",
        source_cluster="dev-cluster",
        requirements=(
            orchestrator_main.StorageSourceRequirementSpec(
                api_resource="core/v1/persistentvolumeclaims",
                namespace="srw",
                requirement_role="quantity",
            ),
        ),
    )
    assert result["state"] == "shadow"
    assert result["effective"] is False
    assert audit.await_args.kwargs["event_type"] == (
        "infrastructure_storage_shadow_entered"
    )


@pytest.mark.asyncio
async def test_storage_activation_schedule_is_generation_fenced(monkeypatch):
    import main as orchestrator_main
    from orchestrator.services.infrastructure_metering.storage_assets import (
        StorageActivation,
        StorageSourceActivation,
    )

    actor_id = uuid4()
    request = MagicMock()
    request.url.path = "/api/admin/usage/v2/storage-activation/claim-requested/schedule"
    monkeypatch.setattr(
        orchestrator_main,
        "_require_infrastructure_fleet_admin",
        AsyncMock(return_value={"id": str(actor_id), "is_admin": True}),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "_infrastructure_leader_generation",
        MagicMock(return_value=9),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        InfrastructureMeteringSettings(
            collector_enabled=True,
            shadow_enabled=True,
            pvc_inventory_enabled=True,
            pvc_shadow_enabled=True,
            stable_cluster_id="dev-cluster",
            namespace_allowlist=("srw",),
        ),
    )
    boundary = datetime(2026, 8, 7, tzinfo=timezone.utc)
    store = MagicMock()
    store.schedule_source_activation = AsyncMock(
        return_value=StorageSourceActivation(
            measurement_basis="claim-requested",
            collector_id="kubernetes-pods",
            source_cluster="dev-cluster",
            state="active",
            activated_at=boundary,
            database_time=boundary - timedelta(hours=1),
        )
    )
    store.read_activations = AsyncMock(
        return_value=(
            StorageActivation(
                "claim-requested",
                "active",
                boundary,
                boundary - timedelta(hours=1),
            ),
            StorageActivation(
                "volume-provisioned",
                "disabled",
                None,
                boundary - timedelta(hours=1),
            ),
        )
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_storage_assets", store)
    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_storage_source_activation_ready",
        True,
    )
    monkeypatch.setattr(orchestrator_main, "log_security_event", AsyncMock())

    await orchestrator_main.schedule_infrastructure_storage_activation(
        request,
        "claim-requested",
        orchestrator_main.InfrastructureStorageActivationScheduleRequest(
            reason="shadow proof reviewed",
            activated_at=boundary,
        ),
    )

    store.schedule_source_activation.assert_awaited_once_with(
        measurement_basis="claim-requested",
        collector_id="kubernetes-pods",
        source_cluster="dev-cluster",
        activated_at=boundary,
        max_scope_age=timedelta(seconds=900),
        expected_generation=9,
        identity_key_version=None,
    )


@pytest.mark.asyncio
async def test_vm_volume_source_shadow_freezes_quantity_and_attribution_scopes(
    monkeypatch,
):
    import main as orchestrator_main
    from orchestrator.services.infrastructure_metering.storage_assets import (
        StorageActivation,
        StorageSourceActivation,
    )

    request = MagicMock()
    request.url.path = (
        "/api/admin/usage/v2/storage-source-activation/vm/volume-provisioned/shadow"
    )
    monkeypatch.setattr(
        orchestrator_main,
        "_require_infrastructure_fleet_admin",
        AsyncMock(return_value={"id": str(uuid4()), "is_admin": True}),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        InfrastructureMeteringSettings(
            collector_enabled=True,
            vm_pvc_inventory_enabled=True,
            vm_pv_inventory_enabled=True,
            vm_stable_cluster_id="vm-dev-cluster",
            vm_namespace="srw-vms",
        ),
    )
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    store = MagicMock()
    store.enter_source_shadow = AsyncMock(
        return_value=StorageSourceActivation(
            measurement_basis="volume-provisioned",
            collector_id="kubevirt-storage",
            source_cluster="vm-dev-cluster",
            state="shadow",
            activated_at=None,
            database_time=now,
        )
    )
    store.read_activations = AsyncMock(
        return_value=(
            StorageActivation("claim-requested", "shadow", None, now),
            StorageActivation("volume-provisioned", "shadow", None, now),
        )
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_storage_assets", store)
    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_storage_source_activation_ready",
        True,
    )
    audit = AsyncMock()
    monkeypatch.setattr(orchestrator_main, "log_security_event", audit)

    result = await orchestrator_main.enter_infrastructure_storage_source_shadow(
        request,
        "vm",
        "volume-provisioned",
        orchestrator_main.InfrastructureStorageActivationRequest(
            reason="remote inventory proof reviewed"
        ),
    )

    store.enter_source_shadow.assert_awaited_once_with(
        measurement_basis="volume-provisioned",
        collector_id="kubevirt-storage",
        source_cluster="vm-dev-cluster",
        requirements=(
            orchestrator_main.StorageSourceRequirementSpec(
                api_resource="core/v1/persistentvolumes",
                namespace=None,
                requirement_role="quantity",
            ),
            orchestrator_main.StorageSourceRequirementSpec(
                api_resource="core/v1/persistentvolumeclaims",
                namespace="srw-vms",
                requirement_role="attribution",
            ),
        ),
    )
    assert result["collector_id"] == "kubevirt-storage"
    assert result["effective"] is False
    assert audit.await_args.kwargs["resource_id"] == "vm:volume-provisioned"


@pytest.mark.asyncio
async def test_compute_activation_shadow_is_class_gated_and_audited(monkeypatch):
    import main as orchestrator_main
    from orchestrator.services.infrastructure_metering.compute_activation import (
        ComputeActivation,
    )

    actor_id = uuid4()
    request = MagicMock()
    request.url.path = "/api/admin/usage/v2/compute-activation/agent_pod/shadow"
    monkeypatch.setattr(
        orchestrator_main,
        "_require_infrastructure_fleet_admin",
        AsyncMock(return_value={"id": str(actor_id), "is_admin": True}),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        InfrastructureMeteringSettings(
            collector_enabled=True,
            stable_cluster_id="dev-cluster",
            namespace_allowlist=("srw",),
        ),
    )
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    store = MagicMock()
    store.enter_shadow = AsyncMock(
        return_value=ComputeActivation(
            activation_key="agent_pod",
            state="shadow",
            activated_at=None,
            database_time=now,
        )
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_compute_activation", store)
    audit = AsyncMock()
    monkeypatch.setattr(orchestrator_main, "log_security_event", audit)

    result = await orchestrator_main.enter_infrastructure_compute_shadow(
        request,
        "agent_pod",
        orchestrator_main.InfrastructureComputeActivationRequest(
            reason="agent shadow soak approved"
        ),
    )

    store.enter_shadow.assert_awaited_once_with("agent_pod")
    assert result["state"] == "shadow"
    assert result["effective"] is False
    assert audit.await_args.kwargs["event_type"] == (
        "infrastructure_compute_shadow_entered"
    )


@pytest.mark.asyncio
async def test_vm_compute_shadow_transition_requires_inventory_not_shadow_config(
    monkeypatch,
):
    import main as orchestrator_main
    from orchestrator.services.infrastructure_metering.compute_activation import (
        ComputeActivation,
    )

    request = MagicMock()
    request.url.path = "/api/admin/usage/v2/compute-activation/workspace_vm/shadow"
    actor_id = uuid4()
    monkeypatch.setattr(
        orchestrator_main,
        "_require_infrastructure_fleet_admin",
        AsyncMock(return_value={"id": str(actor_id), "is_admin": True}),
    )
    store = MagicMock()
    store.enter_shadow = AsyncMock(
        return_value=ComputeActivation(
            activation_key="workspace_vm",
            state="shadow",
            activated_at=None,
            database_time=datetime(2026, 8, 6, 12, tzinfo=timezone.utc),
        )
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_compute_activation", store)
    monkeypatch.setattr(orchestrator_main, "log_security_event", AsyncMock())

    base = InfrastructureMeteringSettings(
        collector_enabled=True,
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_metering_settings", base)
    with pytest.raises(HTTPException) as missing_inventory:
        await orchestrator_main.enter_infrastructure_compute_shadow(
            request,
            "workspace_vm",
            orchestrator_main.InfrastructureComputeActivationRequest(
                reason="inventory must precede durable shadow"
            ),
        )
    assert missing_inventory.value.status_code == 404
    store.enter_shadow.assert_not_awaited()

    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        replace(
            base,
            vm_inventory_enabled=True,
            vm_stable_cluster_id="vm-cluster",
            vm_namespace="agent-vms",
        ),
    )
    result = await orchestrator_main.enter_infrastructure_compute_shadow(
        request,
        "workspace_vm",
        orchestrator_main.InfrastructureComputeActivationRequest(
            reason="inventory verified before enabling shadow config"
        ),
    )
    assert result["state"] == "shadow"
    store.enter_shadow.assert_awaited_once_with("workspace_vm")


@pytest.mark.asyncio
async def test_vm_compute_activation_uses_remote_scope_and_collector(monkeypatch):
    import main as orchestrator_main
    from orchestrator.services.infrastructure_metering.compute_activation import (
        ComputeActivation,
        ComputeActivationScheduleResult,
        ComputeEpochPromotion,
    )

    request = MagicMock()
    request.url.path = "/api/admin/usage/v2/compute-activation/workspace_vm/schedule"
    actor_id = uuid4()
    monkeypatch.setattr(
        orchestrator_main,
        "_require_infrastructure_fleet_admin",
        AsyncMock(return_value={"id": str(actor_id), "is_admin": True}),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "_infrastructure_leader_generation",
        MagicMock(return_value=11),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        InfrastructureMeteringSettings(
            collector_enabled=True,
            shadow_enabled=True,
            vm_inventory_enabled=True,
            vm_shadow_enabled=True,
            stable_cluster_id="dev-cluster",
            namespace_allowlist=("srw",),
            vm_stable_cluster_id="vm-cluster",
            vm_namespace="agent-vms",
        ),
    )
    boundary = datetime(2026, 8, 8, tzinfo=timezone.utc)
    request_id = uuid4()
    store = MagicMock()
    store.schedule_activation = AsyncMock(
        return_value=ComputeActivationScheduleResult(
            activation=ComputeActivation(
                activation_key="workspace_vm",
                state="active",
                activated_at=boundary,
                database_time=boundary - timedelta(hours=1),
            ),
            promotion=ComputeEpochPromotion(
                request_id=request_id,
                activation_key="workspace_vm",
                request_kind="initial-activation",
                promoted_at=boundary - timedelta(hours=1),
                actor_id=actor_id,
                audit_reason="remote VMI shadow proof reviewed",
                replayed=False,
                authorities=(),
            ),
        )
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_compute_activation", store)
    monkeypatch.setattr(orchestrator_main, "log_security_event", AsyncMock())

    await orchestrator_main.schedule_infrastructure_compute_activation(
        request,
        "workspace_vm",
        orchestrator_main.InfrastructureComputeActivationScheduleRequest(
            idempotency_key=request_id,
            reason="remote VMI shadow proof reviewed",
            activated_at=boundary,
        ),
    )

    store.schedule_activation.assert_awaited_once_with(
        activation_key="workspace_vm",
        activated_at=boundary,
        source_cluster="vm-cluster",
        namespaces=("agent-vms",),
        max_scope_age=timedelta(seconds=900),
        expected_generation=11,
        request_id=request_id,
        actor_id=actor_id,
        audit_reason="remote VMI shadow proof reviewed",
        collector_id="kubevirt-vmis",
    )


@pytest.mark.asyncio
async def test_storage_destruction_assertion_is_idempotent_and_audited(monkeypatch):
    import main as orchestrator_main
    from orchestrator.services.infrastructure_metering.storage_assets import (
        BackendDestructionResult,
    )

    actor_id, asset_id, request_id, assertion_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    effective_at = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    request = MagicMock()
    request.url.path = f"/api/admin/usage/v2/storage-assets/{asset_id}/destroy"
    monkeypatch.setattr(
        orchestrator_main,
        "_require_infrastructure_fleet_admin",
        AsyncMock(return_value={"id": str(actor_id), "is_admin": True}),
    )
    store = MagicMock()
    store.assert_destroyed = AsyncMock(
        return_value=BackendDestructionResult(
            assertion_id=assertion_id,
            idempotency_key=request_id,
            asset_id=asset_id,
            effective_at=effective_at,
            request_hash="b" * 64,
            replayed=True,
        )
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_storage_assets", store)
    audit = AsyncMock()
    monkeypatch.setattr(orchestrator_main, "log_security_event", audit)

    body = orchestrator_main.InfrastructureStorageDestructionRequest(
        idempotency_key=request_id,
        effective_at=effective_at,
        evidence_kind="operator-attested",
        evidence_digest="a" * 64,
        reason_code="provider-console-review",
        reason="reviewed by the fleet operator",
    )
    result = await orchestrator_main.assert_infrastructure_storage_asset_destroyed(
        request,
        asset_id,
        body,
    )

    store.assert_destroyed.assert_awaited_once_with(
        idempotency_key=request_id,
        asset_id=asset_id,
        effective_at=effective_at,
        evidence_kind="operator-attested",
        evidence_digest="a" * 64,
        actor_kind="user",
        actor_id=actor_id,
        reason_code="provider-console-review",
    )
    assert result["replayed"] is True
    assert audit.await_args.kwargs["event_type"] == (
        "infrastructure_storage_destruction_replayed"
    )


@pytest.mark.asyncio
async def test_storage_asset_operator_list_and_detail_are_safe_and_bounded(
    monkeypatch,
):
    import main as orchestrator_main
    from orchestrator.services.infrastructure_metering.storage_assets import (
        BackendUnverifiedAssetPage,
        BackendUnverifiedAssetRecord,
        StorageAssetDetailRecord,
        StorageAssetGapDetail,
        StorageAssetIncarnationDetail,
    )

    asset_id, gap_id, epoch_id = uuid4(), uuid4(), uuid4()
    now = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    request = MagicMock()
    request.url.path = "/api/admin/usage/v2/storage-assets/backend-unverified"
    require_admin = AsyncMock(return_value={"id": str(uuid4()), "is_admin": True})
    monkeypatch.setattr(
        orchestrator_main,
        "_require_infrastructure_fleet_admin",
        require_admin,
    )
    record = BackendUnverifiedAssetRecord(
        asset_id=asset_id,
        source_cluster="dev-cluster",
        identity_scheme="csi-hmac-sha256-v1",
        identity_key_version="storage-v1",
        csi_driver="driver.example.test",
        first_observed_at=now - timedelta(hours=1),
        last_observed_at=now - timedelta(minutes=5),
        backend_unverified_at=now,
        gap_id=gap_id,
        gap_start=now,
        reason_code="retain-pv-absent",
        storage_class_name="standard",
        reclaim_policy="retain",
        backend_deletion_finalizer_observed=True,
        volume_mode="filesystem",
        capacity_bytes=4 * 1024**3,
        detached_at=now,
        detach_reason="pv-deleted",
    )
    detail = StorageAssetDetailRecord(
        asset_id=asset_id,
        source_cluster="dev-cluster",
        identity_scheme="csi-hmac-sha256-v1",
        identity_key_version="storage-v1",
        csi_driver="driver.example.test",
        lifecycle_state="backend-unverified",
        first_observed_at=now - timedelta(hours=1),
        last_observed_at=now - timedelta(minutes=5),
        backend_unverified_at=now,
        destroyed_at=None,
        incarnations=(
            StorageAssetIncarnationDetail(
                storage_class_name="standard",
                reclaim_policy="retain",
                backend_deletion_finalizer_observed=True,
                volume_mode="filesystem",
                capacity_bytes=4 * 1024**3,
                first_observed_at=now - timedelta(hours=1),
                last_observed_at=now - timedelta(minutes=5),
                detached_at=now,
                detach_reason="pv-deleted",
            ),
        ),
        gaps=(
            StorageAssetGapDetail(
                gap_id=gap_id,
                scope_epoch_id=epoch_id,
                gap_start=now,
                gap_end=None,
                reason_code="retain-pv-absent",
                resolution="unresolved",
                resolution_assertion_id=None,
                resolved_at=None,
            ),
        ),
        assertions=(),
        history_truncated=False,
    )
    store = MagicMock()
    store.list_backend_unverified = AsyncMock(
        return_value=BackendUnverifiedAssetPage(items=(record,), next_cursor=asset_id)
    )
    store.read_asset_detail = AsyncMock(return_value=detail)
    monkeypatch.setattr(orchestrator_main, "infrastructure_storage_assets", store)

    listed = (
        await orchestrator_main.list_infrastructure_backend_unverified_storage_assets(
            request,
            limit=25,
            cursor=None,
        )
    )
    rendered = await orchestrator_main.get_infrastructure_storage_asset_detail(
        request,
        asset_id,
        history_limit=25,
    )

    store.list_backend_unverified.assert_awaited_once_with(
        limit=25,
        after_asset_id=None,
    )
    store.read_asset_detail.assert_awaited_once_with(
        asset_id=asset_id,
        history_limit=25,
    )
    assert listed["items"][0]["asset_id"] == asset_id
    assert rendered["gaps"][0]["gap_id"] == gap_id
    serialized = repr((listed, rendered)).lower()
    assert "volume_handle" not in serialized
    assert "volume_attributes" not in serialized
    assert require_admin.await_count == 2
