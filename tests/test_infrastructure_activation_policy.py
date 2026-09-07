"""Direct coverage for the pure infrastructure-metering activation policy.

Most of this module's behavior is exercised indirectly through the lifespan
wiring and the admin routes. What is pinned here is what those two paths cannot
see between them: that the module is a *pure* decision over its arguments, that
"effective" and "durable" stay distinct, and the two helpers whose only previous
caller was ``lifespan`` itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services import infrastructure_activation_policy as policy
from orchestrator.services.infrastructure_metering.compute_activation import (
    ComputeActivation,
)
from orchestrator.services.infrastructure_metering.config import (
    InfrastructureMeteringSettings,
)
from orchestrator.services.infrastructure_metering.storage_assets import (
    StorageActivation,
    StorageSourceActivation,
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
BEFORE = NOW - timedelta(hours=1)
AFTER = NOW + timedelta(hours=1)


def _settings(**overrides) -> InfrastructureMeteringSettings:
    base = dict(
        collector_enabled=True,
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw", "srw-ops"),
        vm_stable_cluster_id="vm-cluster",
        vm_namespace="srw-vms",
    )
    base.update(overrides)
    return InfrastructureMeteringSettings(**base)


class TestEffectiveVersusDurable:
    def test_a_scheduled_boundary_is_durable_before_the_clock_reaches_it(self):
        """History exists from the moment the boundary is written, not after."""
        scheduled = StorageActivation("claim-requested", "active", AFTER, NOW)
        assert policy.storage_activation_is_durable(scheduled) is True
        assert policy.storage_activation_is_effective(scheduled) is False

    def test_effectiveness_needs_the_database_clock_to_have_passed_it(self):
        crossed = StorageActivation("claim-requested", "active", BEFORE, NOW)
        assert policy.storage_activation_is_effective(crossed) is True

    def test_a_source_is_never_effective_without_its_global_master(self):
        source = StorageSourceActivation(
            measurement_basis="claim-requested",
            collector_id="kubernetes-pods",
            source_cluster="dev-cluster",
            state="active",
            activated_at=BEFORE,
            database_time=NOW,
        )
        assert policy.storage_source_activation_is_effective(source, None) is False
        disabled = StorageActivation("claim-requested", "disabled", None, NOW)
        assert policy.storage_source_activation_is_effective(source, disabled) is False
        active = StorageActivation("claim-requested", "active", BEFORE, NOW)
        assert policy.storage_source_activation_is_effective(source, active) is True

    def test_a_shadow_compute_class_is_neither_durable_nor_effective(self):
        shadow = ComputeActivation("agent_pod", "shadow", None, NOW)
        assert policy.compute_activation_is_durable(shadow) is False
        assert policy.compute_activation_is_effective(shadow) is False


class TestDurableReportingFailsClosed:
    def _capabilities(self, **overrides) -> MagicMock:
        base = dict(
            slice3_compute_inventory_ready=True,
            slice3_storage_lifecycle_ready=True,
            slice2_volume_inventory_ready=True,
            storage_identity_key_version="storage-v1",
        )
        base.update(overrides)
        return MagicMock(**base)

    def test_a_durable_class_without_its_schema_raises_rather_than_omitting(self):
        """Silently dropping a durable class hides history it already published."""
        durable = {"agent_pod": ComputeActivation("agent_pod", "active", BEFORE, NOW)}
        with pytest.raises(ValueError, match="compute reporting schema"):
            policy.durable_infrastructure_reporting_resources(
                self._capabilities(slice3_compute_inventory_ready=False),
                compute_activations=durable,
            )

    def test_the_workspace_pod_baseline_is_always_present(self):
        assert policy.durable_infrastructure_reporting_resources(
            self._capabilities()
        ) == ("workspace_pod",)


class TestComputeScopeConfiguration:
    def test_the_vm_class_resolves_to_the_remote_cluster_and_collector(self):
        assert policy.compute_scope_configuration("workspace_vm", _settings()) == (
            "vm-cluster",
            ("srw-vms",),
            "kubevirt-vmis",
        )

    @pytest.mark.parametrize("key", ["agent_pod", "ide_workspace_pod"])
    def test_pod_classes_resolve_to_the_local_allowlist(self, key):
        assert policy.compute_scope_configuration(key, _settings()) == (
            "dev-cluster",
            ("srw", "srw-ops"),
            "kubernetes-pods",
        )


class TestStorageSourceContract:
    def test_a_claim_source_needs_one_quantity_scope_per_namespace(self):
        collector, cluster, requirements = policy.storage_source_configuration(
            "primary", "claim-requested", _settings()
        )
        assert (collector, cluster) == ("kubernetes-pods", "dev-cluster")
        assert [r.namespace for r in requirements] == ["srw", "srw-ops"]
        assert {r.requirement_role for r in requirements} == {"quantity"}

    def test_a_volume_source_measures_cluster_wide_and_attributes_per_namespace(self):
        _c, _s, requirements = policy.storage_source_configuration(
            "vm", "volume-provisioned", _settings()
        )
        roles = [
            (r.api_resource, r.namespace, r.requirement_role) for r in requirements
        ]
        assert roles == [
            ("core/v1/persistentvolumes", None, "quantity"),
            ("core/v1/persistentvolumeclaims", "srw-vms", "attribution"),
        ]

    def test_a_widened_namespace_allowlist_breaks_the_frozen_scope_set(self):
        settings = _settings(pvc_inventory_enabled=True, pvc_shadow_enabled=True)
        _c, _s, frozen = policy.storage_source_configuration(
            "primary", "claim-requested", settings
        )
        activation = StorageSourceActivation(
            measurement_basis="claim-requested",
            collector_id="kubernetes-pods",
            source_cluster="dev-cluster",
            state="shadow",
            activated_at=None,
            requirements=tuple(
                MagicMock(
                    api_resource=r.api_resource,
                    namespace=r.namespace,
                    requirement_role=r.requirement_role,
                )
                for r in frozen
            ),
            database_time=NOW,
        )
        assert policy.storage_source_configuration_errors(settings, (activation,)) == ()

        widened = _settings(
            pvc_inventory_enabled=True,
            pvc_shadow_enabled=True,
            namespace_allowlist=("srw", "srw-ops", "newcomer"),
        )
        assert policy.storage_source_configuration_errors(widened, (activation,)) == (
            "primary/claim-requested frozen inventory scope set",
        )

    def test_a_volume_basis_needs_both_claim_and_volume_inventory(self):
        claims_only = _settings(pvc_inventory_enabled=True, pv_inventory_enabled=False)
        assert (
            policy.storage_source_inventory_enabled(
                "primary", "claim-requested", claims_only
            )
            is True
        )
        assert (
            policy.storage_source_inventory_enabled(
                "primary", "volume-provisioned", claims_only
            )
            is False
        )


class TestDurableCollectionSettings:
    def test_an_active_class_keeps_its_shadow_writer_armed(self):
        """Otherwise a stale collector's mode mismatch freezes coverage."""
        settings = _settings(collector_enabled=True, shadow_enabled=False)
        armed = policy.durable_collection_settings(
            settings,
            compute_activations={
                "agent_pod": ComputeActivation("agent_pod", "active", BEFORE, NOW)
            },
        )
        assert armed.shadow_enabled is True
        assert armed.agent_pod_shadow_enabled is True
        assert armed.publication_enabled is False

    def test_nothing_durable_returns_the_very_same_settings_object(self):
        settings = _settings()
        assert policy.durable_collection_settings(settings) is settings


class TestWorkspaceMeteringAttribution:
    @pytest.mark.asyncio
    async def test_a_job_owner_is_read_from_the_jobs_table(self):
        store = MagicMock()
        store.get_job = AsyncMock(return_value={"user_id": "u", "project_id": "p"})
        store.get_thread = AsyncMock(
            side_effect=AssertionError("threads read for a job owner")
        )
        assert await policy.workspace_metering_attribution(
            "job", "job-1", store=store
        ) == {"user_id": "u", "project_id": "p"}

    @pytest.mark.asyncio
    async def test_any_other_owner_kind_is_read_from_the_threads_table(self):
        store = MagicMock()
        store.get_thread = AsyncMock(return_value={"user_id": "u", "project_id": None})
        store.get_job = AsyncMock(
            side_effect=AssertionError("jobs read for a thread owner")
        )
        assert await policy.workspace_metering_attribution(
            "thread", "thread-1", store=store
        ) == {"user_id": "u", "project_id": None}

    @pytest.mark.asyncio
    async def test_a_deleted_owner_yields_none_rather_than_dropping_the_row(self):
        store = MagicMock()
        store.get_job = AsyncMock(return_value=None)
        assert (
            await policy.workspace_metering_attribution("job", "gone", store=store)
            is None
        )

    @pytest.mark.asyncio
    async def test_a_store_failure_is_swallowed_so_metering_keeps_running(self):
        """Attribution is best-effort; the compute row is still worth recording."""
        store = MagicMock()
        store.get_job = AsyncMock(side_effect=RuntimeError("pool closed"))
        assert (
            await policy.workspace_metering_attribution("job", "job-1", store=store)
            is None
        )
