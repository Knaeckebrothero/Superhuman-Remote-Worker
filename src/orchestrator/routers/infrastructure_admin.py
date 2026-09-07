"""HTTP adapters for infrastructure-metering administration and ingestion.

Two boundaries share this router and must not be confused. The
``/api/admin/usage/v2/*`` routes are fleet-admin operations: they go through
``_require_infrastructure_fleet_admin``, which is strictly narrower than the
plain admin gate — a project-scoped MCP admin is rejected with a 403 because an
activation boundary is fleet-wide and irreversible. The
``/api/internal/infrastructure-metering/v1/*`` routes carry no user identity at
all; ``_dispatch_infrastructure_ingestion`` authenticates the collector's
HMAC-signed method, path, body digest, timestamp and nonce before any work.

Both helpers stay module-local and keep those exact names: the endpoint
inventory classifies a route by the name of the gate it calls, and these two
names carry the ``admin:`` and ``internal:`` labels for every route below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from orchestrator.schemas.infrastructure_admin import (
    InfrastructureComputeActivationRequest,
    InfrastructureComputeActivationScheduleRequest,
    InfrastructureComputeEpochRolloverRequest,
    InfrastructureCorrectionRequest,
    InfrastructureCoverageWaiverRequest,
    InfrastructureCutoverPrepareRequest,
    InfrastructureStorageActivationRequest,
    InfrastructureStorageActivationScheduleRequest,
    InfrastructureStorageDestructionRequest,
)
from orchestrator.services import infrastructure_admin

router = APIRouter()


@dataclass(frozen=True)
class InfrastructureAdminDependencies:
    """Per-app admin gate plus the metering stores this request may reach."""

    operations: infrastructure_admin.InfrastructureAdminDependencies
    require_admin: Callable[..., Awaitable[Any]]


def get_infrastructure_admin_dependencies(
    request: Request,
) -> InfrastructureAdminDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.infrastructure_admin_dependencies_factory()


async def _require_infrastructure_fleet_admin(
    request: Request, dependencies: InfrastructureAdminDependencies
) -> dict[str, Any]:
    """Require an unscoped admin acting with the real fleet view."""

    admin = await dependencies.require_admin(request)
    return await infrastructure_admin.enforce_fleet_admin(
        request, admin, dependencies=dependencies.operations
    )


async def _dispatch_infrastructure_ingestion(
    operation: Literal[
        "ticket",
        "snapshot_begin",
        "snapshot_items",
        "snapshot_finalize",
        "watch_apply",
        "watch_finish",
    ],
    request: Request,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    return await infrastructure_admin.dispatch_ingestion(
        operation, request, dependencies=dependencies.operations
    )


@router.get("/api/admin/usage/v2/storage-activation")
async def get_infrastructure_storage_activation(
    request: Request,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    await _require_infrastructure_fleet_admin(request, dependencies)
    return await infrastructure_admin.storage_activation_status(
        dependencies=dependencies.operations
    )


async def _enter_infrastructure_storage_source_shadow(
    request: Request,
    source: Literal["primary", "vm"],
    measurement_basis: Literal["claim-requested", "volume-provisioned"],
    body: InfrastructureStorageActivationRequest,
    *,
    event_type: str,
    resource_id: str,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    admin = await _require_infrastructure_fleet_admin(request, dependencies)
    return await infrastructure_admin.enter_storage_source_shadow(
        request,
        admin,
        source,
        measurement_basis,
        body,
        event_type=event_type,
        resource_id=resource_id,
        dependencies=dependencies.operations,
    )


@router.post("/api/admin/usage/v2/storage-activation/{measurement_basis}/shadow")
async def enter_infrastructure_storage_shadow(
    request: Request,
    measurement_basis: Literal["claim-requested", "volume-provisioned"],
    body: InfrastructureStorageActivationRequest,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    """Compatibility route for the primary-cluster storage authority."""

    return await _enter_infrastructure_storage_source_shadow(
        request,
        "primary",
        measurement_basis,
        body,
        event_type="infrastructure_storage_shadow_entered",
        resource_id=measurement_basis,
        dependencies=dependencies,
    )


@router.post(
    "/api/admin/usage/v2/storage-source-activation/{source}/{measurement_basis}/shadow"
)
async def enter_infrastructure_storage_source_shadow(
    request: Request,
    source: Literal["primary", "vm"],
    measurement_basis: Literal["claim-requested", "volume-provisioned"],
    body: InfrastructureStorageActivationRequest,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    return await _enter_infrastructure_storage_source_shadow(
        request,
        source,
        measurement_basis,
        body,
        event_type="infrastructure_storage_source_shadow_entered",
        resource_id=f"{source}:{measurement_basis}",
        dependencies=dependencies,
    )


async def _schedule_infrastructure_storage_source_activation(
    request: Request,
    source: Literal["primary", "vm"],
    measurement_basis: Literal["claim-requested", "volume-provisioned"],
    body: InfrastructureStorageActivationScheduleRequest,
    *,
    event_type: str,
    resource_id: str,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    admin = await _require_infrastructure_fleet_admin(request, dependencies)
    return await infrastructure_admin.schedule_storage_source_activation(
        request,
        admin,
        source,
        measurement_basis,
        body,
        event_type=event_type,
        resource_id=resource_id,
        dependencies=dependencies.operations,
    )


@router.post("/api/admin/usage/v2/storage-activation/{measurement_basis}/schedule")
async def schedule_infrastructure_storage_activation(
    request: Request,
    measurement_basis: Literal["claim-requested", "volume-provisioned"],
    body: InfrastructureStorageActivationScheduleRequest,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    """Compatibility route for the primary-cluster storage authority."""

    return await _schedule_infrastructure_storage_source_activation(
        request,
        "primary",
        measurement_basis,
        body,
        event_type="infrastructure_storage_activation_scheduled",
        resource_id=measurement_basis,
        dependencies=dependencies,
    )


@router.post(
    "/api/admin/usage/v2/storage-source-activation/"
    "{source}/{measurement_basis}/schedule"
)
async def schedule_infrastructure_storage_source_activation(
    request: Request,
    source: Literal["primary", "vm"],
    measurement_basis: Literal["claim-requested", "volume-provisioned"],
    body: InfrastructureStorageActivationScheduleRequest,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    return await _schedule_infrastructure_storage_source_activation(
        request,
        source,
        measurement_basis,
        body,
        event_type="infrastructure_storage_source_activation_scheduled",
        resource_id=f"{source}:{measurement_basis}",
        dependencies=dependencies,
    )


@router.get("/api/admin/usage/v2/compute-activation")
async def get_infrastructure_compute_activation(
    request: Request,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    await _require_infrastructure_fleet_admin(request, dependencies)
    return await infrastructure_admin.compute_activation_status(
        dependencies=dependencies.operations
    )


@router.post("/api/admin/usage/v2/compute-activation/{activation_key}/shadow")
async def enter_infrastructure_compute_shadow(
    request: Request,
    activation_key: Literal["agent_pod", "ide_workspace_pod", "workspace_vm"],
    body: InfrastructureComputeActivationRequest,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    admin = await _require_infrastructure_fleet_admin(request, dependencies)
    return await infrastructure_admin.enter_compute_shadow(
        request,
        admin,
        activation_key,
        body,
        dependencies=dependencies.operations,
    )


@router.post("/api/admin/usage/v2/compute-activation/{activation_key}/schedule")
async def schedule_infrastructure_compute_activation(
    request: Request,
    activation_key: Literal["agent_pod", "ide_workspace_pod", "workspace_vm"],
    body: InfrastructureComputeActivationScheduleRequest,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    admin = await _require_infrastructure_fleet_admin(request, dependencies)
    return await infrastructure_admin.schedule_compute_activation(
        request,
        admin,
        activation_key,
        body,
        dependencies=dependencies.operations,
    )


@router.post("/api/admin/usage/v2/compute-activation/{activation_key}/rollover")
async def rollover_infrastructure_compute_epoch(
    request: Request,
    activation_key: Literal["agent_pod", "ide_workspace_pod", "workspace_vm"],
    body: InfrastructureComputeEpochRolloverRequest,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    """Promote fresh post-recovery epochs without inheriting a retired epoch."""

    admin = await _require_infrastructure_fleet_admin(request, dependencies)
    return await infrastructure_admin.rollover_compute_epoch(
        request,
        admin,
        activation_key,
        body,
        dependencies=dependencies.operations,
    )


@router.get("/api/admin/usage/v2/storage-assets/backend-unverified")
async def list_infrastructure_backend_unverified_storage_assets(
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: UUID | None = Query(default=None),
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    await _require_infrastructure_fleet_admin(request, dependencies)
    return await infrastructure_admin.list_backend_unverified_storage_assets(
        limit=limit,
        cursor=cursor,
        dependencies=dependencies.operations,
    )


@router.get("/api/admin/usage/v2/storage-assets/{asset_id}")
async def get_infrastructure_storage_asset_detail(
    request: Request,
    asset_id: UUID,
    history_limit: int = Query(default=100, ge=1, le=200),
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    await _require_infrastructure_fleet_admin(request, dependencies)
    return await infrastructure_admin.storage_asset_detail(
        asset_id=asset_id,
        history_limit=history_limit,
        dependencies=dependencies.operations,
    )


@router.post("/api/admin/usage/v2/storage-assets/{asset_id}/destroy")
async def assert_infrastructure_storage_asset_destroyed(
    request: Request,
    asset_id: UUID,
    body: InfrastructureStorageDestructionRequest,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    admin = await _require_infrastructure_fleet_admin(request, dependencies)
    return await infrastructure_admin.assert_storage_asset_destroyed(
        request,
        admin,
        asset_id,
        body,
        dependencies=dependencies.operations,
    )


@router.get("/api/admin/usage/v2/infrastructure-cutover")
async def get_infrastructure_metering_cutover(
    request: Request,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    await _require_infrastructure_fleet_admin(request, dependencies)
    return await infrastructure_admin.metering_cutover_status(
        dependencies=dependencies.operations
    )


@router.post("/api/admin/usage/v2/infrastructure-cutover/prepare")
async def prepare_infrastructure_metering_cutover(
    request: Request,
    body: InfrastructureCutoverPrepareRequest,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    admin = await _require_infrastructure_fleet_admin(request, dependencies)
    return await infrastructure_admin.prepare_metering_cutover(
        request,
        admin,
        body,
        dependencies=dependencies.operations,
    )


@router.post("/api/admin/usage/v2/coverage-gaps/{gap_id}/waive")
async def waive_infrastructure_metering_coverage_gap(
    request: Request,
    gap_id: UUID,
    body: InfrastructureCoverageWaiverRequest,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    admin = await _require_infrastructure_fleet_admin(request, dependencies)
    return await infrastructure_admin.waive_coverage_gap(
        request,
        admin,
        gap_id,
        body,
        dependencies=dependencies.operations,
    )


@router.post("/api/admin/usage/v2/infrastructure-corrections")
async def create_infrastructure_metering_correction(
    request: Request,
    body: InfrastructureCorrectionRequest,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    admin = await _require_infrastructure_fleet_admin(request, dependencies)
    return await infrastructure_admin.create_metering_correction(
        request,
        admin,
        body,
        dependencies=dependencies.operations,
    )


@router.post(
    "/api/internal/infrastructure-metering/v1/tickets",
    include_in_schema=False,
)
async def infrastructure_metering_ticket(
    request: Request,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    return await _dispatch_infrastructure_ingestion("ticket", request, dependencies)


@router.post(
    "/api/internal/infrastructure-metering/v1/snapshots/begin",
    include_in_schema=False,
)
async def infrastructure_metering_snapshot_begin(
    request: Request,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    return await _dispatch_infrastructure_ingestion(
        "snapshot_begin", request, dependencies
    )


@router.post(
    "/api/internal/infrastructure-metering/v1/snapshots/items",
    include_in_schema=False,
)
async def infrastructure_metering_snapshot_items(
    request: Request,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    return await _dispatch_infrastructure_ingestion(
        "snapshot_items", request, dependencies
    )


@router.post(
    "/api/internal/infrastructure-metering/v1/snapshots/finalize",
    include_in_schema=False,
)
async def infrastructure_metering_snapshot_finalize(
    request: Request,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    return await _dispatch_infrastructure_ingestion(
        "snapshot_finalize", request, dependencies
    )


@router.post(
    "/api/internal/infrastructure-metering/v1/watch/apply",
    include_in_schema=False,
)
async def infrastructure_metering_watch_apply(
    request: Request,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    return await _dispatch_infrastructure_ingestion(
        "watch_apply", request, dependencies
    )


@router.post(
    "/api/internal/infrastructure-metering/v1/watch/finish",
    include_in_schema=False,
)
async def infrastructure_metering_watch_finish(
    request: Request,
    *,
    dependencies: InfrastructureAdminDependencies = Depends(
        get_infrastructure_admin_dependencies
    ),
) -> dict[str, Any]:
    return await _dispatch_infrastructure_ingestion(
        "watch_finish", request, dependencies
    )
