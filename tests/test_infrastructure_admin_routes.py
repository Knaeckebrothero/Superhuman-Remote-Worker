"""Wire contracts for the extracted infrastructure-metering admin router.

Two boundaries live in one router and must stay distinguishable: the
``/api/admin/usage/v2/*`` fleet-admin operations, which are strictly narrower
than the plain admin gate, and the ``/api/internal/infrastructure-metering/*``
collector boundary, which carries no user identity at all.

The activation refusals are the point of most of these tests. Each rung of the
one-way ladder has its own status code, and collapsing any two of them would
make an operator unable to tell "this class was never configured" from "the
store is down" from "somebody else moved it first".
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.routers import infrastructure_admin as routes
from orchestrator.services import infrastructure_admin as operations
from orchestrator.services.infrastructure_metering.compute_activation import (
    ComputeActivation,
    ComputeActivationConflict,
    ComputeActivationScheduleResult,
    ComputeEpochPromotion,
)
from orchestrator.services.infrastructure_metering.config import (
    InfrastructureMeteringSettings,
)
from orchestrator.services.infrastructure_metering.coverage import CoverageGapNotFound
from orchestrator.services.infrastructure_metering.ingestion_http import (
    IngestionRequestError,
)
from orchestrator.services.infrastructure_metering.storage_assets import (
    StorageActivation,
    StorageSourceActivation,
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
FLEET_ADMIN = {"id": str(uuid4()), "is_admin": True, "real_is_admin": True}

_PRIMARY = InfrastructureMeteringSettings(
    collector_enabled=True,
    pvc_inventory_enabled=True,
    stable_cluster_id="dev-cluster",
    namespace_allowlist=("srw",),
)


def _operations(
    *,
    settings: InfrastructureMeteringSettings | None = None,
    audit: AsyncMock | None = None,
    **overrides: Any,
) -> operations.InfrastructureAdminDependencies:
    fields: dict[str, Any] = {
        "store": MagicMock(),
        "logger": logging.getLogger("test-infrastructure-admin-router"),
        "settings": settings if settings is not None else _PRIMARY,
        "audit": audit if audit is not None else AsyncMock(),
        "leader_generation": lambda: 7,
        "scope_project_id": lambda _user: None,
    }
    fields.update(overrides)
    return operations.InfrastructureAdminDependencies(**fields)


def _dependencies(
    *,
    admin: dict[str, Any] | None = None,
    require_admin: AsyncMock | None = None,
    **overrides: Any,
) -> routes.InfrastructureAdminDependencies:
    return routes.InfrastructureAdminDependencies(
        operations=_operations(**overrides),
        require_admin=require_admin
        or AsyncMock(return_value=admin if admin is not None else FLEET_ADMIN),
    )


def _client(dependencies) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    factory = dependencies if callable(dependencies) else (lambda: dependencies)
    app.state.infrastructure_admin_dependencies_factory = factory
    return TestClient(app, raise_server_exceptions=False)


def _shadow_source() -> StorageSourceActivation:
    return StorageSourceActivation(
        measurement_basis="claim-requested",
        collector_id="kubernetes-pods",
        source_cluster="dev-cluster",
        state="shadow",
        activated_at=None,
        database_time=NOW,
    )


def _global_activations() -> tuple[StorageActivation, StorageActivation]:
    return (
        StorageActivation("claim-requested", "shadow", None, NOW),
        StorageActivation("volume-provisioned", "disabled", None, NOW),
    )


class TestFleetAdminGate:
    def test_a_project_scoped_mcp_admin_is_refused_and_audited(self):
        """Being an admin somewhere is not authority over a fleet-wide boundary."""
        audit = AsyncMock()
        deps = _dependencies(audit=audit, scope_project_id=lambda _user: uuid4())
        with _client(deps) as client:
            response = client.get("/api/admin/usage/v2/storage-activation")
        assert response.status_code == 403
        assert response.json()["detail"] == "Fleet admin access required"
        assert audit.await_args.kwargs["event_type"] == "admin_denied"

    def test_a_shadowed_admin_view_is_refused(self):
        audit = AsyncMock()
        deps = _dependencies(
            admin={"id": str(uuid4()), "real_is_admin": True, "is_admin": False},
            audit=audit,
        )
        with _client(deps) as client:
            response = client.get("/api/admin/usage/v2/compute-activation")
        assert response.status_code == 403
        assert audit.await_count == 1

    def test_a_real_fleet_admin_reaches_the_store(self):
        audit = AsyncMock()
        store = MagicMock()
        store.read_activations = AsyncMock(return_value=_global_activations())
        store.source_status = AsyncMock(return_value=(_shadow_source(),))
        deps = _dependencies(
            audit=audit,
            storage_assets=store,
            storage_source_activation_ready=True,
        )
        with _client(deps) as client:
            body = client.get("/api/admin/usage/v2/storage-activation").json()
        assert body["source_activation_ready"] is True
        assert body["source_activations"][0]["state"] == "shadow"
        assert body["source_activations"][0]["effective"] is False
        audit.assert_not_awaited()

    def test_source_status_is_not_read_until_its_schema_is_ready(self):
        store = MagicMock()
        store.read_activations = AsyncMock(return_value=_global_activations())
        store.source_status = AsyncMock(
            side_effect=AssertionError("source status read before readiness")
        )
        deps = _dependencies(
            storage_assets=store, storage_source_activation_ready=False
        )
        with _client(deps) as client:
            body = client.get("/api/admin/usage/v2/storage-activation").json()
        assert body["source_activations"] == []
        assert body["source_activation_ready"] is False


class TestStorageActivationRefusals:
    def test_shadow_needs_inventory_for_that_exact_source_and_basis(self):
        store = MagicMock()
        store.enter_source_shadow = AsyncMock(
            side_effect=AssertionError("shadow entered without inventory")
        )
        deps = _dependencies(
            settings=InfrastructureMeteringSettings(collector_enabled=True),
            storage_assets=store,
            storage_source_activation_ready=True,
        )
        with _client(deps) as client:
            response = client.post(
                "/api/admin/usage/v2/storage-activation/claim-requested/shadow",
                json={"reason": "inventory is not on yet"},
            )
        assert response.status_code == 404
        assert "inventory is not enabled" in response.json()["detail"]

    def test_shadow_is_unavailable_until_the_activation_schema_is_ready(self):
        deps = _dependencies(
            storage_assets=MagicMock(),
            storage_source_activation_ready=False,
        )
        with _client(deps) as client:
            response = client.post(
                "/api/admin/usage/v2/storage-activation/claim-requested/shadow",
                json={"reason": "schema is still migrating"},
            )
        assert response.status_code == 503

    def test_scheduling_needs_shadow_first(self):
        """Inventory alone is not proof; the shadow soak is the evidence."""
        store = MagicMock()
        store.schedule_source_activation = AsyncMock(
            side_effect=AssertionError("scheduled without a shadow soak")
        )
        deps = _dependencies(
            settings=_PRIMARY,
            storage_assets=store,
            storage_source_activation_ready=True,
        )
        with _client(deps) as client:
            response = client.post(
                "/api/admin/usage/v2/storage-activation/claim-requested/schedule",
                json={
                    "reason": "no shadow proof exists",
                    "activated_at": NOW.isoformat(),
                },
            )
        assert response.status_code == 404
        assert "shadow mode is not enabled" in response.json()["detail"]

    def test_the_vm_source_route_carries_its_own_scoped_identity(self):
        store = MagicMock()
        store.enter_source_shadow = AsyncMock(return_value=_shadow_source())
        store.read_activations = AsyncMock(return_value=_global_activations())
        audit = AsyncMock()
        deps = _dependencies(
            settings=InfrastructureMeteringSettings(
                collector_enabled=True,
                vm_pvc_inventory_enabled=True,
                vm_stable_cluster_id="vm-cluster",
                vm_namespace="srw-vms",
            ),
            audit=audit,
            storage_assets=store,
            storage_source_activation_ready=True,
        )
        with _client(deps) as client:
            response = client.post(
                "/api/admin/usage/v2/storage-source-activation/vm/"
                "claim-requested/shadow",
                json={"reason": "remote inventory reviewed"},
            )
        assert response.status_code == 200
        assert store.enter_source_shadow.await_args.kwargs["collector_id"] == (
            "kubevirt-storage"
        )
        assert audit.await_args.kwargs["resource_id"] == "vm:claim-requested"


class TestComputeActivationRefusals:
    def test_shadow_needs_inventory_for_that_class(self):
        store = MagicMock()
        store.enter_shadow = AsyncMock(
            side_effect=AssertionError("shadow entered without inventory")
        )
        deps = _dependencies(
            settings=InfrastructureMeteringSettings(),
            compute_activation=store,
        )
        with _client(deps) as client:
            response = client.post(
                "/api/admin/usage/v2/compute-activation/agent_pod/shadow",
                json={"reason": "collector is off"},
            )
        assert response.status_code == 404

    def test_a_missing_store_is_a_503_not_a_500(self):
        deps = _dependencies(settings=_PRIMARY, compute_activation=None)
        with _client(deps) as client:
            response = client.post(
                "/api/admin/usage/v2/compute-activation/agent_pod/shadow",
                json={"reason": "store is not wired"},
            )
        assert response.status_code == 503

    def test_a_racing_writer_becomes_a_409(self):
        store = MagicMock()
        store.enter_shadow = AsyncMock(
            side_effect=ComputeActivationConflict("already shadowing")
        )
        deps = _dependencies(settings=_PRIMARY, compute_activation=store)
        with _client(deps) as client:
            response = client.post(
                "/api/admin/usage/v2/compute-activation/agent_pod/shadow",
                json={"reason": "two operators at once"},
            )
        assert response.status_code == 409

    def test_a_durably_active_class_stays_schedulable_with_its_helm_flag_off(self):
        """Turning a Helm boolean off must not strand an already-active class."""
        boundary = NOW + timedelta(days=1)
        promotion = ComputeEpochPromotion(
            request_id=uuid4(),
            activation_key="agent_pod",
            request_kind="initial-activation",
            promoted_at=NOW,
            actor_id=uuid4(),
            audit_reason="reviewed",
            replayed=False,
            authorities=(),
        )
        store = MagicMock()
        store.schedule_activation = AsyncMock(
            return_value=ComputeActivationScheduleResult(
                activation=ComputeActivation(
                    activation_key="agent_pod",
                    state="active",
                    activated_at=boundary,
                    database_time=NOW,
                ),
                promotion=promotion,
            )
        )
        deps = _dependencies(
            settings=_PRIMARY,  # agent_pod_shadow_enabled is False here
            compute_activation=store,
            durable_compute_activation_keys=frozenset({"agent_pod"}),
        )
        with _client(deps) as client:
            response = client.post(
                "/api/admin/usage/v2/compute-activation/agent_pod/schedule",
                json={
                    "idempotency_key": str(uuid4()),
                    "reason": "durable class stays mutable",
                    "activated_at": boundary.isoformat(),
                },
            )
        assert response.status_code == 200
        assert response.json()["promotion"]["request_kind"] == "initial-activation"

    def test_rollover_needs_shadow_and_reports_the_promotion(self):
        store = MagicMock()
        store.promote_recovery_epochs = AsyncMock(
            side_effect=AssertionError("rolled over without shadow")
        )
        deps = _dependencies(
            settings=InfrastructureMeteringSettings(collector_enabled=True),
            compute_activation=store,
        )
        with _client(deps) as client:
            response = client.post(
                "/api/admin/usage/v2/compute-activation/agent_pod/rollover",
                json={"idempotency_key": str(uuid4()), "reason": "no shadow"},
            )
        assert response.status_code == 404

    def test_the_leader_fence_is_a_retryable_409(self):
        """Two processes moving the same boundary is worse than a retry."""
        store = MagicMock()
        store.schedule_activation = AsyncMock(
            side_effect=AssertionError("scheduled without leadership")
        )
        deps = _dependencies(
            settings=_PRIMARY,
            compute_activation=store,
            durable_compute_activation_keys=frozenset({"agent_pod"}),
            leader_generation=operations.infrastructure_leader_generation,
        )
        with _client(deps) as client:
            response = client.post(
                "/api/admin/usage/v2/compute-activation/agent_pod/schedule",
                json={
                    "idempotency_key": str(uuid4()),
                    "reason": "not the leader",
                    "activated_at": NOW.isoformat(),
                },
            )
        assert response.status_code == 409
        assert response.headers["Retry-After"] == "1"


class TestCutoverAndCorrections:
    def test_cutover_status_is_unavailable_without_its_coordinator(self):
        deps = _dependencies(workspace_cutover=None)
        with _client(deps) as client:
            response = client.get("/api/admin/usage/v2/infrastructure-cutover")
        assert response.status_code == 503

    def test_prepare_is_gated_before_the_reporting_policy_is_checked(self):
        coordinator = MagicMock()
        coordinator.prepare = AsyncMock(
            side_effect=AssertionError("prepared while the gate was off")
        )
        deps = _dependencies(
            settings=InfrastructureMeteringSettings(),
            workspace_cutover=coordinator,
            durable_reporting_policy_ready=True,
        )
        with _client(deps) as client:
            response = client.post(
                "/api/admin/usage/v2/infrastructure-cutover/prepare",
                json={"idempotency_key": str(uuid4()), "reason": "gate is off"},
            )
        assert response.status_code == 404

    def test_prepare_refuses_while_the_historical_read_policy_is_unusable(self):
        coordinator = MagicMock()
        coordinator.prepare = AsyncMock(
            side_effect=AssertionError("prepared without a read policy")
        )
        deps = _dependencies(
            settings=InfrastructureMeteringSettings(cutover_enabled=True),
            workspace_cutover=coordinator,
            durable_reporting_policy_ready=False,
        )
        with _client(deps) as client:
            response = client.post(
                "/api/admin/usage/v2/infrastructure-cutover/prepare",
                json={"idempotency_key": str(uuid4()), "reason": "policy is dark"},
            )
        assert response.status_code == 503

    def test_a_correction_needs_publication_to_be_on(self):
        materializer = MagicMock()
        materializer.create_correction = AsyncMock(
            side_effect=AssertionError("corrected unpublished history")
        )
        deps = _dependencies(
            settings=InfrastructureMeteringSettings(),
            usage_materializer=materializer,
        )
        with _client(deps) as client:
            response = client.post(
                "/api/admin/usage/v2/infrastructure-corrections",
                json={
                    "idempotency_key": str(uuid4()),
                    "reason": "publication is off",
                    "deltas": [
                        {
                            "source": "infra-allocation-v2",
                            "source_id": "sid",
                            "unit": "vcpu-hour",
                            "ts": NOW.isoformat(),
                            "expected_payload_hash": "c" * 64,
                            "quantity": "1",
                        }
                    ],
                },
            )
        assert response.status_code == 404

    def test_an_unknown_coverage_gap_is_a_404(self):
        waivers = MagicMock()
        waivers.waive = AsyncMock(side_effect=CoverageGapNotFound("no such gap"))
        deps = _dependencies(coverage_waivers=waivers)
        with _client(deps) as client:
            response = client.post(
                f"/api/admin/usage/v2/coverage-gaps/{uuid4()}/waive",
                json={"idempotency_key": str(uuid4()), "reason": "already closed"},
            )
        assert response.status_code == 404


class TestStorageAssetDestruction:
    @pytest.mark.parametrize(
        "error",
        [
            asyncpg.CheckViolationError("lifecycle"),
            asyncpg.UniqueViolationError("replayed hash"),
            asyncpg.ObjectNotInPrerequisiteStateError("not detached"),
        ],
    )
    def test_a_lifecycle_constraint_is_a_409_not_a_500(self, error):
        """The database constraints are part of the contract, not a server fault."""
        store = MagicMock()
        store.assert_destroyed = AsyncMock(side_effect=error)
        deps = _dependencies(storage_assets=store)
        with _client(deps) as client:
            response = client.post(
                f"/api/admin/usage/v2/storage-assets/{uuid4()}/destroy",
                json={
                    "idempotency_key": str(uuid4()),
                    "effective_at": NOW.isoformat(),
                    "evidence_kind": "operator-attested",
                    "evidence_digest": "a" * 64,
                    "reason_code": "provider-console-review",
                    "reason": "reviewed in the provider console",
                },
            )
        assert response.status_code == 409

    def test_a_dangling_asset_reference_is_a_404(self):
        store = MagicMock()
        store.assert_destroyed = AsyncMock(
            side_effect=asyncpg.ForeignKeyViolationError("no asset")
        )
        deps = _dependencies(storage_assets=store)
        with _client(deps) as client:
            response = client.post(
                f"/api/admin/usage/v2/storage-assets/{uuid4()}/destroy",
                json={
                    "idempotency_key": str(uuid4()),
                    "effective_at": NOW.isoformat(),
                    "evidence_kind": "operator-attested",
                    "evidence_digest": "a" * 64,
                    "reason_code": "provider-console-review",
                    "reason": "reviewed in the provider console",
                },
            )
        assert response.status_code == 404
        assert response.json()["detail"] == "Storage asset not found"


_INGESTION_PATHS = [
    ("/api/internal/infrastructure-metering/v1/tickets", "ticket"),
    ("/api/internal/infrastructure-metering/v1/snapshots/begin", "snapshot_begin"),
    ("/api/internal/infrastructure-metering/v1/snapshots/items", "snapshot_items"),
    (
        "/api/internal/infrastructure-metering/v1/snapshots/finalize",
        "snapshot_finalize",
    ),
    ("/api/internal/infrastructure-metering/v1/watch/apply", "watch_apply"),
    ("/api/internal/infrastructure-metering/v1/watch/finish", "watch_finish"),
]


class TestInternalIngestion:
    def test_the_collector_boundary_never_consults_the_admin_gate(self):
        gate = AsyncMock(side_effect=AssertionError("user auth on a collector call"))
        service = SimpleNamespace(
            ticket=AsyncMock(return_value={"granted": True}),
        )
        deps = _dependencies(require_admin=gate, ingestion_service=service)
        with _client(deps) as client:
            response = client.post(
                "/api/internal/infrastructure-metering/v1/tickets", json={}
            )
        assert response.status_code == 200
        assert response.json() == {"granted": True}

    @pytest.mark.parametrize("path,operation", _INGESTION_PATHS)
    def test_every_operation_routes_to_its_own_handler(self, path, operation):
        service = SimpleNamespace(
            **{
                name: AsyncMock(
                    side_effect=AssertionError(f"{name} handled {operation}")
                )
                for _p, name in _INGESTION_PATHS
            }
        )
        setattr(service, operation, AsyncMock(return_value={"op": operation}))
        deps = _dependencies(ingestion_service=service)
        with _client(deps) as client:
            response = client.post(path, json={})
        assert response.status_code == 200
        assert response.json() == {"op": operation}

    def test_an_unwired_service_is_a_retryable_503(self):
        deps = _dependencies(ingestion_service=None)
        with _client(deps) as client:
            response = client.post(
                "/api/internal/infrastructure-metering/v1/tickets", json={}
            )
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"

    def test_a_rejected_signature_reports_only_its_code(self):
        """The collector boundary must not echo why authentication failed."""
        service = SimpleNamespace(
            watch_apply=AsyncMock(
                side_effect=IngestionRequestError(401, "invalid ingestion signature")
            )
        )
        deps = _dependencies(ingestion_service=service)
        with _client(deps) as client:
            response = client.post(
                "/api/internal/infrastructure-metering/v1/watch/apply", json={}
            )
        assert response.status_code == 401
        assert response.json()["detail"] == "invalid ingestion signature"
        assert "Retry-After" not in response.headers

    def test_a_generation_conflict_asks_the_collector_to_retry(self):
        service = SimpleNamespace(
            snapshot_items=AsyncMock(
                side_effect=IngestionRequestError(409, "inventory generation conflict")
            )
        )
        deps = _dependencies(ingestion_service=service)
        with _client(deps) as client:
            response = client.post(
                "/api/internal/infrastructure-metering/v1/snapshots/items", json={}
            )
        assert response.status_code == 409
        assert response.headers["Retry-After"] == "1"


class TestFactoryResolution:
    def test_stores_are_resolved_per_request(self):
        """They are ``None`` at import and appear only during lifespan."""
        unwired = _dependencies(storage_assets=None)
        store = MagicMock()
        store.read_activations = AsyncMock(return_value=_global_activations())
        wired = _dependencies(storage_assets=store)
        seen: list[int] = []

        def factory():
            seen.append(len(seen))
            return unwired if len(seen) == 1 else wired

        with _client(factory) as client:
            assert (
                client.get("/api/admin/usage/v2/storage-activation").status_code == 503
            )
            assert (
                client.get("/api/admin/usage/v2/storage-activation").status_code == 200
            )
        assert len(seen) == 2


def test_every_admin_route_goes_through_the_narrower_fleet_gate():
    """A regression guard on the narrower fleet check, in source terms.

    ``scripts/check_endpoint_auth.py`` labels these routes by the *name* of the
    helper they call. A route that called ``deps.require_admin`` directly would
    still read as an admin route in the inventory while silently accepting a
    project-scoped MCP admin, so the label alone cannot catch that regression.
    """
    import inspect

    forwarders = {
        "_enter_infrastructure_storage_source_shadow",
        "_schedule_infrastructure_storage_source_activation",
    }
    for route in routes.router.routes:
        if not route.path.startswith("/api/admin/"):
            continue
        source = inspect.getsource(route.endpoint)
        assert "dependencies.require_admin(" not in source, route.path
        reached = "_require_infrastructure_fleet_admin(" in source or any(
            name in source for name in forwarders
        )
        assert reached, route.path

    for name in forwarders:
        source = inspect.getsource(getattr(routes, name))
        assert "_require_infrastructure_fleet_admin(" in source


def test_backend_unverified_is_declared_before_the_asset_id_route():
    """Otherwise the literal segment is parsed as a UUID and 422s."""
    paths = [route.path for route in routes.router.routes]
    assert paths.index(
        "/api/admin/usage/v2/storage-assets/backend-unverified"
    ) < paths.index("/api/admin/usage/v2/storage-assets/{asset_id}")
