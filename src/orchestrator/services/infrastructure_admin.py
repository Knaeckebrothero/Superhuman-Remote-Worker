"""Fleet-admin operations over the infrastructure-metering activation ladder.

Every class of metered infrastructure climbs the same one-way ladder —
``disabled`` → ``shadow`` → scheduled ``active`` — and each rung is guarded
here rather than in the HTTP layer, so the refusals are testable without a
request. Three properties this module exists to keep:

* **One-way means one-way.** Scheduling an activation writes an irreversible
  boundary. The preconditions (inventory enabled before shadow, shadow enabled
  before scheduling, a live leader generation before either) are checked in
  that order and each has its own status code: 404 for a class whose source is
  not configured at all, 409 for a race or a lost leadership fence, 422 for a
  request that contradicts the frozen contract.
* **The store is the authority, not the process.** Reads and writes go to the
  activation stores; this layer only maps their exceptions onto HTTP and folds
  their records into payloads. Nothing caches an activation across requests.
* **Every mutation is logged before it is returned.** ``log_security_event``
  runs on the success path too, because an activation boundary that moved
  without a named actor is indistinguishable from a bug.

Collaborators arrive through :class:`InfrastructureAdminDependencies`. All of
them are assigned during application startup and are ``None`` at import, so the
application must build this fresh per request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID

import asyncpg
from fastapi import HTTPException, Request

from orchestrator.schemas.infrastructure_admin import (
    InfrastructureCorrectionRequest,
    InfrastructureCoverageWaiverRequest,
    InfrastructureCutoverPrepareRequest,
    InfrastructureComputeActivationRequest,
    InfrastructureComputeActivationScheduleRequest,
    InfrastructureComputeEpochRolloverRequest,
    InfrastructureStorageActivationRequest,
    InfrastructureStorageActivationScheduleRequest,
    InfrastructureStorageDestructionRequest,
)
from orchestrator.security.access import log_security_event, mcp_scope_project_id
from orchestrator.services import infrastructure_activation_policy as activation_policy
from orchestrator.services.infrastructure_metering import InfrastructureMeteringSettings
from orchestrator.services.infrastructure_metering.compute_activation import (
    ComputeActivation,
    ComputeActivationConflict,
    ComputeActivationContractError,
    ComputeActivationNotReady,
    ComputeEpochAuthority,
    ComputeEpochPromotion,
    compute_scope_configuration_diagnostic,
)
from orchestrator.services.infrastructure_metering.coverage import (
    CoverageGapConflict,
    CoverageGapContractError,
    CoverageGapNotFound,
)
from orchestrator.services.infrastructure_metering.cutover import (
    CutoverBlocked,
    CutoverConflictError,
    CutoverContractError,
    CutoverFenceError,
    CutoverStatus,
)
from orchestrator.services.infrastructure_metering.ingestion import (
    dispatch_ingestion_request,
)
from orchestrator.services.infrastructure_metering.ingestion_http import (
    IngestionRequestError,
)
from orchestrator.services.infrastructure_metering.materializer import (
    CorrectionRequestDelta,
    PublicationConflictError,
    PublicationContractError,
    PublicationDisabledError,
    PublicationFenceError,
)
from orchestrator.services.infrastructure_metering.storage_assets import (
    BackendDestructionResult,
    BackendUnverifiedAssetPage,
    StorageActivation,
    StorageActivationNotReady,
    StorageAssetConflict,
    StorageAssetContractError,
    StorageAssetDetailRecord,
    StorageAssetNotFound,
    StorageSourceActivation,
)
from orchestrator.services.usage_ledger import (
    StrictUsageConflict,
    StrictUsageLedgerError,
)

ComputeActivationKey = Literal["agent_pod", "ide_workspace_pod", "workspace_vm"]
StorageSource = Literal["primary", "vm"]
MeasurementBasis = Literal["claim-requested", "volume-provisioned"]

IngestionOperation = Literal[
    "ticket",
    "snapshot_begin",
    "snapshot_items",
    "snapshot_finalize",
    "watch_apply",
    "watch_finish",
]


def infrastructure_leader_generation() -> int:
    """The current leader fence, or a retryable 409 when leadership is absent.

    Every one-way write is fenced on this generation. Serving one from a
    replica that is not the leader would let two processes move the same
    boundary, so the refusal is deliberate and retryable rather than a 500.
    """
    from orchestrator.services.leader_election import get_leader_generation, is_leader

    generation = get_leader_generation()
    if not is_leader.is_set() or generation is None:
        raise HTTPException(
            status_code=409,
            detail="Infrastructure metering leader is unavailable; retry",
            headers={"Retry-After": "1"},
        )
    return generation


@dataclass(frozen=True)
class InfrastructureAdminDependencies:
    """Per-request collaborators; every metering store may still be ``None``.

    ``durable_compute_activation_keys``, ``durable_reporting_policy_ready`` and
    ``storage_source_activation_ready`` are wiring facts settled during startup;
    they are values here so a request reads the state the process actually
    booted with rather than re-deriving it.
    """

    store: Any
    logger: Any
    settings: InfrastructureMeteringSettings
    durable_compute_activation_keys: frozenset[str] = field(default_factory=frozenset)
    durable_reporting_policy_ready: bool = False
    storage_source_activation_ready: bool = False
    storage_assets: Any | None = None
    compute_activation: Any | None = None
    workspace_cutover: Any | None = None
    usage_materializer: Any | None = None
    coverage_waivers: Any | None = None
    ingestion_service: Any | None = None
    leader_generation: Callable[[], int] = infrastructure_leader_generation
    audit: Callable[..., Awaitable[Any]] = log_security_event
    scope_project_id: Callable[[dict[str, Any]], Any] = mcp_scope_project_id


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


async def enforce_fleet_admin(
    request: Request,
    admin: dict[str, Any],
    *,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    """Reject a project-scoped MCP admin that already cleared the admin gate.

    Activation boundaries are fleet-wide and irreversible, so an admin whose
    token is narrowed to one project must not be able to move them — being an
    admin somewhere is not the same authority as seeing the whole fleet.
    """
    if not admin.get("is_admin") or dependencies.scope_project_id(admin) is not None:
        await dependencies.audit(
            dependencies.store,
            event_type="admin_denied",
            user=admin,
            resource_type="admin_endpoint",
            resource_id=getattr(getattr(request, "url", None), "path", None),
            detail="Fleet admin access required",
            request=request,
        )
        raise HTTPException(status_code=403, detail="Fleet admin access required")
    return admin


# ---------------------------------------------------------------------------
# Internal collector ingestion
# ---------------------------------------------------------------------------


async def dispatch_ingestion(
    operation: IngestionOperation,
    request: Request,
    *,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    """Authenticate and apply one HMAC-signed collector call."""
    try:
        return await dispatch_ingestion_request(
            dependencies.ingestion_service,
            operation,
            request,
        )
    except IngestionRequestError as exc:
        headers = {"Retry-After": "1"} if exc.status_code in {409, 503} else None
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
            headers=headers,
        ) from exc


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def cutover_status_payload(status: CutoverStatus) -> dict[str, Any]:
    return {
        "state": status.state,
        "phase": status.phase.value,
        "leader_generation": status.leader_generation,
        "cutover_at": status.cutover_at,
        "request_id": status.request_id,
        "actor_id": status.actor_id,
        "reason": status.reason,
        "unplanned_intervals": status.unplanned_intervals,
        "planned": status.planned,
        "published": status.published,
        "conflicts": status.conflicts,
        "open_legacy_intervals": status.open_legacy_intervals,
        "cutover_error": status.cutover_error,
    }


def storage_activation_payload(activation: StorageActivation) -> dict[str, Any]:
    return {
        "measurement_basis": activation.measurement_basis,
        "state": activation.state,
        "activated_at": activation.activated_at,
        "database_time": activation.database_time,
        "effective": activation_policy.storage_activation_is_effective(activation),
    }


def storage_source_activation_payload(
    activation: StorageSourceActivation,
    global_activation: StorageActivation | None,
) -> dict[str, Any]:
    return {
        "measurement_basis": activation.measurement_basis,
        "collector_id": activation.collector_id,
        "source_cluster": activation.source_cluster,
        "state": activation.state,
        "activated_at": activation.activated_at,
        "database_time": activation.database_time,
        "effective": activation_policy.storage_source_activation_is_effective(
            activation,
            global_activation,
        ),
        "requirements": [
            {
                "inventory_scope_id": requirement.inventory_scope_id,
                "api_resource": requirement.api_resource,
                "namespace": requirement.namespace,
                "role": requirement.requirement_role,
            }
            for requirement in activation.requirements
        ],
    }


def compute_activation_payload(activation: ComputeActivation) -> dict[str, Any]:
    effective = (
        activation.state == "active"
        and activation.activated_at is not None
        and activation.database_time is not None
        and activation.database_time >= activation.activated_at
    )
    return {
        "activation_key": activation.activation_key,
        "state": activation.state,
        "activated_at": activation.activated_at,
        "database_time": activation.database_time,
        "effective": effective,
    }


def compute_epoch_authority_payload(
    authority: ComputeEpochAuthority,
) -> dict[str, Any]:
    return {
        "id": authority.id,
        "activation_key": authority.activation_key,
        "collector_id": authority.collector_id,
        "source_cluster": authority.source_cluster,
        "inventory_scope_id": authority.inventory_scope_id,
        "inventory_scope_epoch_id": authority.inventory_scope_epoch_id,
        "previous_authority_id": authority.previous_authority_id,
        "predecessor_epoch_id": authority.predecessor_epoch_id,
        "authority_sequence": authority.authority_sequence,
        "effective_from": authority.effective_from,
        "effective_to": authority.effective_to,
        "proof_snapshot_id": authority.proof_snapshot_id,
        "proof_generation": authority.proof_generation,
        "promotion_request_id": authority.promotion_request_id,
        "namespace": authority.namespace,
        "is_current_epoch": authority.is_current_epoch,
    }


def compute_epoch_promotion_payload(
    promotion: ComputeEpochPromotion,
) -> dict[str, Any]:
    return {
        "request_id": promotion.request_id,
        "activation_key": promotion.activation_key,
        "request_kind": promotion.request_kind,
        "promoted_at": promotion.promoted_at,
        "actor_id": promotion.actor_id,
        "audit_reason": promotion.audit_reason,
        "replayed": promotion.replayed,
        "authorities": [
            compute_epoch_authority_payload(authority)
            for authority in promotion.authorities
        ],
    }


def storage_destruction_payload(
    result: BackendDestructionResult,
) -> dict[str, Any]:
    return {
        "assertion_id": result.assertion_id,
        "idempotency_key": result.idempotency_key,
        "asset_id": result.asset_id,
        "effective_at": result.effective_at,
        "request_hash": result.request_hash,
        "replayed": result.replayed,
    }


def backend_unverified_storage_payload(
    page: BackendUnverifiedAssetPage,
) -> dict[str, Any]:
    return {
        "items": [
            {
                "asset_id": item.asset_id,
                "source_cluster": item.source_cluster,
                "identity_scheme": item.identity_scheme,
                "identity_key_version": item.identity_key_version,
                "csi_driver": item.csi_driver,
                "first_observed_at": item.first_observed_at,
                "last_observed_at": item.last_observed_at,
                "backend_unverified_at": item.backend_unverified_at,
                "gap_id": item.gap_id,
                "gap_start": item.gap_start,
                "reason_code": item.reason_code,
                "storage_class_name": item.storage_class_name,
                "reclaim_policy": item.reclaim_policy,
                "backend_deletion_finalizer_observed": (
                    item.backend_deletion_finalizer_observed
                ),
                "volume_mode": item.volume_mode,
                "capacity_bytes": item.capacity_bytes,
                "detached_at": item.detached_at,
                "detach_reason": item.detach_reason,
            }
            for item in page.items
        ],
        "next_cursor": page.next_cursor,
    }


def storage_asset_detail_payload(
    item: StorageAssetDetailRecord,
) -> dict[str, Any]:
    return {
        "asset_id": item.asset_id,
        "source_cluster": item.source_cluster,
        "identity_scheme": item.identity_scheme,
        "identity_key_version": item.identity_key_version,
        "csi_driver": item.csi_driver,
        "lifecycle_state": item.lifecycle_state,
        "first_observed_at": item.first_observed_at,
        "last_observed_at": item.last_observed_at,
        "backend_unverified_at": item.backend_unverified_at,
        "destroyed_at": item.destroyed_at,
        "history_truncated": item.history_truncated,
        "incarnations": [
            {
                "storage_class_name": row.storage_class_name,
                "reclaim_policy": row.reclaim_policy,
                "backend_deletion_finalizer_observed": (
                    row.backend_deletion_finalizer_observed
                ),
                "volume_mode": row.volume_mode,
                "capacity_bytes": row.capacity_bytes,
                "first_observed_at": row.first_observed_at,
                "last_observed_at": row.last_observed_at,
                "detached_at": row.detached_at,
                "detach_reason": row.detach_reason,
            }
            for row in item.incarnations
        ],
        "gaps": [
            {
                "gap_id": row.gap_id,
                "scope_epoch_id": row.scope_epoch_id,
                "gap_start": row.gap_start,
                "gap_end": row.gap_end,
                "reason_code": row.reason_code,
                "resolution": row.resolution,
                "resolution_assertion_id": row.resolution_assertion_id,
                "resolved_at": row.resolved_at,
            }
            for row in item.gaps
        ],
        "assertions": [
            {
                "assertion_id": row.assertion_id,
                "effective_at": row.effective_at,
                "evidence_kind": row.evidence_kind,
                "evidence_digest": row.evidence_digest,
                "actor_kind": row.actor_kind,
                "actor_id": row.actor_id,
                "reason_code": row.reason_code,
                "created_at": row.created_at,
            }
            for row in item.assertions
        ],
    }


# ---------------------------------------------------------------------------
# Per-class configuration gates
# ---------------------------------------------------------------------------


def compute_class_shadow_enabled(
    activation_key: str,
    *,
    dependencies: InfrastructureAdminDependencies,
) -> bool:
    if activation_key in dependencies.durable_compute_activation_keys:
        return True
    if activation_key == "agent_pod":
        return dependencies.settings.agent_pod_shadow_enabled
    if activation_key == "ide_workspace_pod":
        return dependencies.settings.ide_pod_shadow_enabled
    return dependencies.settings.vm_shadow_enabled


def compute_class_inventory_enabled(
    activation_key: str,
    *,
    dependencies: InfrastructureAdminDependencies,
) -> bool:
    if activation_key == "workspace_vm":
        return dependencies.settings.vm_inventory_enabled
    return dependencies.settings.collector_enabled


def compute_class_publication_enabled(
    activation_key: str,
    *,
    dependencies: InfrastructureAdminDependencies,
) -> bool:
    if activation_key == "agent_pod":
        return dependencies.settings.agent_pod_publication_enabled
    if activation_key == "ide_workspace_pod":
        return dependencies.settings.ide_pod_publication_enabled
    return dependencies.settings.vm_publication_enabled


def storage_basis_inventory_enabled(
    measurement_basis: MeasurementBasis,
    *,
    dependencies: InfrastructureAdminDependencies,
) -> bool:
    return activation_policy.storage_source_inventory_enabled(
        "primary", measurement_basis, dependencies.settings
    )


def storage_basis_shadow_enabled(
    measurement_basis: MeasurementBasis,
    *,
    dependencies: InfrastructureAdminDependencies,
) -> bool:
    return activation_policy.storage_source_shadow_enabled(
        "primary", measurement_basis, dependencies.settings
    )


# ---------------------------------------------------------------------------
# Storage activation
# ---------------------------------------------------------------------------


async def storage_activation_status(
    *, dependencies: InfrastructureAdminDependencies
) -> dict[str, Any]:
    """Global + exact-source storage activation, with the configured gates."""
    settings = dependencies.settings
    storage_assets = dependencies.storage_assets
    if storage_assets is None:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure storage metering is unavailable",
        )
    try:
        claim, volume = await storage_assets.read_activations()
        source_activations = (
            await storage_assets.source_status()
            if dependencies.storage_source_activation_ready
            else ()
        )
    except Exception as exc:
        dependencies.logger.exception("Infrastructure storage activation status failed")
        raise HTTPException(
            status_code=500,
            detail="Infrastructure storage activation status failed",
        ) from exc
    return {
        "activations": [
            storage_activation_payload(claim),
            storage_activation_payload(volume),
        ],
        "source_activations": [
            storage_source_activation_payload(
                activation,
                claim if activation.measurement_basis == "claim-requested" else volume,
            )
            for activation in source_activations
        ],
        "source_activation_ready": dependencies.storage_source_activation_ready,
        "configuration": {
            "claim_inventory_enabled": settings.pvc_inventory_enabled,
            "claim_shadow_enabled": settings.pvc_shadow_enabled,
            "claim_publication_enabled": settings.pvc_publication_enabled,
            "volume_inventory_enabled": settings.pv_inventory_enabled,
            "volume_shadow_enabled": settings.pv_shadow_enabled,
            "volume_publication_enabled": settings.pv_publication_enabled,
            "sources": {
                source: {
                    basis: {
                        "inventory_enabled": (
                            activation_policy.storage_source_inventory_enabled(
                                source,
                                basis,
                                settings,
                            )
                        ),
                        "shadow_enabled": (
                            activation_policy.storage_source_shadow_enabled(
                                source,
                                basis,
                                settings,
                            )
                        ),
                        "publication_enabled": (
                            activation_policy.storage_source_publication_enabled(
                                source, basis, settings
                            )
                        ),
                    }
                    for basis in ("claim-requested", "volume-provisioned")
                }
                for source in ("primary", "vm")
            },
        },
    }


async def enter_storage_source_shadow(
    request: Request,
    admin: dict[str, Any],
    source: StorageSource,
    measurement_basis: MeasurementBasis,
    body: InfrastructureStorageActivationRequest,
    *,
    event_type: str,
    resource_id: str,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    """Move one exact storage source from disabled to shadow ingestion."""
    settings = dependencies.settings
    storage_assets = dependencies.storage_assets
    if not activation_policy.storage_source_inventory_enabled(
        source, measurement_basis, settings
    ):
        raise HTTPException(
            status_code=404,
            detail=(
                "Infrastructure storage inventory is not enabled for this "
                "source and basis"
            ),
        )
    if storage_assets is None or not dependencies.storage_source_activation_ready:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure storage source activation is unavailable",
        )
    (
        collector_id,
        source_cluster,
        requirements,
    ) = activation_policy.storage_source_configuration(
        source,
        measurement_basis,
        settings,
    )
    try:
        activation = await storage_assets.enter_source_shadow(
            measurement_basis=measurement_basis,
            collector_id=collector_id,
            source_cluster=source_cluster,
            requirements=requirements,
        )
        claim, volume = await storage_assets.read_activations()
    except StorageAssetContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (StorageActivationNotReady, StorageAssetConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        dependencies.logger.exception("Infrastructure storage shadow transition failed")
        raise HTTPException(
            status_code=500,
            detail="Infrastructure storage shadow transition failed",
        ) from exc
    await dependencies.audit(
        dependencies.store,
        event_type=event_type,
        user=admin,
        resource_type="infrastructure_storage_activation",
        resource_id=resource_id,
        detail=(
            f"collector_id={collector_id} source_cluster={source_cluster} "
            f"state={activation.state} reason={body.reason}"
        ),
        request=request,
    )
    return storage_source_activation_payload(
        activation,
        claim if measurement_basis == "claim-requested" else volume,
    )


async def schedule_storage_source_activation(
    request: Request,
    admin: dict[str, Any],
    source: StorageSource,
    measurement_basis: MeasurementBasis,
    body: InfrastructureStorageActivationScheduleRequest,
    *,
    event_type: str,
    resource_id: str,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    """Write the irreversible activation boundary for one exact storage source."""
    settings = dependencies.settings
    storage_assets = dependencies.storage_assets
    if not activation_policy.storage_source_shadow_enabled(
        source, measurement_basis, settings
    ):
        raise HTTPException(
            status_code=404,
            detail=(
                "Infrastructure storage shadow mode is not enabled for this "
                "source and basis"
            ),
        )
    if storage_assets is None or not dependencies.storage_source_activation_ready:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure storage source activation is unavailable",
        )
    (
        collector_id,
        source_cluster,
        _requirements,
    ) = activation_policy.storage_source_configuration(
        source,
        measurement_basis,
        settings,
    )
    generation = dependencies.leader_generation()
    try:
        activation = await storage_assets.schedule_source_activation(
            measurement_basis=measurement_basis,
            collector_id=collector_id,
            source_cluster=source_cluster,
            activated_at=body.activated_at,
            max_scope_age=timedelta(seconds=settings.stale_after_seconds),
            expected_generation=generation,
            identity_key_version=(
                settings.volume_identity_key_version
                if measurement_basis == "volume-provisioned"
                else None
            ),
        )
        claim, volume = await storage_assets.read_activations()
    except StorageAssetContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (StorageActivationNotReady, StorageAssetConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        dependencies.logger.exception(
            "Infrastructure storage activation scheduling failed"
        )
        raise HTTPException(
            status_code=500,
            detail="Infrastructure storage activation scheduling failed",
        ) from exc
    await dependencies.audit(
        dependencies.store,
        event_type=event_type,
        user=admin,
        resource_type="infrastructure_storage_activation",
        resource_id=resource_id,
        detail=(
            f"collector_id={collector_id} source_cluster={source_cluster} "
            f"activated_at={activation.activated_at} reason={body.reason}"
        ),
        request=request,
    )
    return storage_source_activation_payload(
        activation,
        claim if measurement_basis == "claim-requested" else volume,
    )


# ---------------------------------------------------------------------------
# Compute activation
# ---------------------------------------------------------------------------


async def compute_activation_status(
    *, dependencies: InfrastructureAdminDependencies
) -> dict[str, Any]:
    """Per-class compute activation, epoch authorities and scope diagnostics."""
    compute_activation = dependencies.compute_activation
    if compute_activation is None:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure compute metering is unavailable",
        )
    try:
        activations = await compute_activation.status()
        requirements = await compute_activation.requirements()
        authorities = await compute_activation.authorities()
    except Exception as exc:
        dependencies.logger.exception("Infrastructure compute activation status failed")
        raise HTTPException(
            status_code=500,
            detail="Infrastructure compute activation status failed",
        ) from exc
    diagnostics: dict[str, str] = {}
    for activation in activations:
        (
            source_cluster,
            namespaces,
            collector_id,
        ) = activation_policy.compute_scope_configuration(
            activation.activation_key, dependencies.settings
        )
        diagnostic = compute_scope_configuration_diagnostic(
            activation,
            requirements,
            source_cluster=source_cluster,
            namespaces=namespaces,
            collector_id=collector_id,
            authorities=authorities,
        )
        if diagnostic is not None:
            diagnostics[activation.activation_key] = diagnostic
    return {
        "activations": [
            compute_activation_payload(activation) for activation in activations
        ],
        "epoch_authorities": [
            compute_epoch_authority_payload(authority) for authority in authorities
        ],
        "configuration": {
            activation.activation_key: {
                "inventory_enabled": compute_class_inventory_enabled(
                    activation.activation_key, dependencies=dependencies
                ),
                "shadow_enabled": compute_class_shadow_enabled(
                    activation.activation_key, dependencies=dependencies
                ),
                "publication_enabled": compute_class_publication_enabled(
                    activation.activation_key, dependencies=dependencies
                ),
                "scope_compatible": (activation.activation_key not in diagnostics),
                "scope_diagnostic": diagnostics.get(activation.activation_key),
            }
            for activation in activations
        },
    }


async def enter_compute_shadow(
    request: Request,
    admin: dict[str, Any],
    activation_key: ComputeActivationKey,
    body: InfrastructureComputeActivationRequest,
    *,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    """Move one compute class from disabled to shadow ingestion."""
    compute_activation = dependencies.compute_activation
    if not compute_class_inventory_enabled(activation_key, dependencies=dependencies):
        raise HTTPException(
            status_code=404,
            detail="Infrastructure compute inventory is not enabled for this class",
        )
    if compute_activation is None:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure compute metering is unavailable",
        )
    try:
        activation = await compute_activation.enter_shadow(activation_key)
    except ComputeActivationContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ComputeActivationNotReady, ComputeActivationConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        dependencies.logger.exception("Infrastructure compute shadow transition failed")
        raise HTTPException(
            status_code=500,
            detail="Infrastructure compute shadow transition failed",
        ) from exc
    await dependencies.audit(
        dependencies.store,
        event_type="infrastructure_compute_shadow_entered",
        user=admin,
        resource_type="infrastructure_compute_activation",
        resource_id=activation_key,
        detail=f"state={activation.state} reason={body.reason}",
        request=request,
    )
    return compute_activation_payload(activation)


async def schedule_compute_activation(
    request: Request,
    admin: dict[str, Any],
    activation_key: ComputeActivationKey,
    body: InfrastructureComputeActivationScheduleRequest,
    *,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    """Write the irreversible activation boundary for one compute class."""
    settings = dependencies.settings
    compute_activation = dependencies.compute_activation
    if not compute_class_shadow_enabled(activation_key, dependencies=dependencies):
        raise HTTPException(
            status_code=404,
            detail="Infrastructure compute shadow mode is not enabled for this class",
        )
    if compute_activation is None:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure compute metering is unavailable",
        )
    source_cluster, namespaces, collector_id = (
        activation_policy.compute_scope_configuration(activation_key, settings)
    )
    generation = dependencies.leader_generation()
    try:
        scheduled = await compute_activation.schedule_activation(
            activation_key=activation_key,
            activated_at=body.activated_at,
            source_cluster=source_cluster,
            namespaces=namespaces,
            max_scope_age=timedelta(seconds=settings.stale_after_seconds),
            expected_generation=generation,
            request_id=body.idempotency_key,
            actor_id=UUID(str(admin["id"])),
            audit_reason=body.reason,
            collector_id=collector_id,
        )
    except ComputeActivationContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ComputeActivationNotReady, ComputeActivationConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        dependencies.logger.exception(
            "Infrastructure compute activation scheduling failed"
        )
        raise HTTPException(
            status_code=500,
            detail="Infrastructure compute activation scheduling failed",
        ) from exc
    await dependencies.audit(
        dependencies.store,
        event_type="infrastructure_compute_activation_scheduled",
        user=admin,
        resource_type="infrastructure_compute_activation",
        resource_id=activation_key,
        detail=(
            f"request_id={scheduled.promotion.request_id} "
            f"activated_at={scheduled.activation.activated_at} "
            f"replayed={scheduled.promotion.replayed} reason={body.reason}"
        ),
        request=request,
    )
    payload = compute_activation_payload(scheduled.activation)
    payload["promotion"] = compute_epoch_promotion_payload(scheduled.promotion)
    return payload


async def rollover_compute_epoch(
    request: Request,
    admin: dict[str, Any],
    activation_key: ComputeActivationKey,
    body: InfrastructureComputeEpochRolloverRequest,
    *,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    """Promote fresh post-recovery epochs without inheriting a retired epoch."""
    settings = dependencies.settings
    compute_activation = dependencies.compute_activation
    if not compute_class_shadow_enabled(activation_key, dependencies=dependencies):
        raise HTTPException(
            status_code=404,
            detail="Infrastructure compute shadow mode is not enabled for this class",
        )
    if compute_activation is None:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure compute metering is unavailable",
        )
    source_cluster, namespaces, collector_id = (
        activation_policy.compute_scope_configuration(activation_key, settings)
    )
    generation = dependencies.leader_generation()
    try:
        promotion = await compute_activation.promote_recovery_epochs(
            activation_key=activation_key,
            source_cluster=source_cluster,
            namespaces=namespaces,
            max_scope_age=timedelta(seconds=settings.stale_after_seconds),
            expected_generation=generation,
            request_id=body.idempotency_key,
            actor_id=UUID(str(admin["id"])),
            audit_reason=body.reason,
            collector_id=collector_id,
        )
    except ComputeActivationContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ComputeActivationNotReady, ComputeActivationConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        dependencies.logger.exception("Infrastructure compute epoch rollover failed")
        raise HTTPException(
            status_code=500,
            detail="Infrastructure compute epoch rollover failed",
        ) from exc
    await dependencies.audit(
        dependencies.store,
        event_type="infrastructure_compute_epoch_rolled_over",
        user=admin,
        resource_type="infrastructure_compute_epoch_authority",
        resource_id=activation_key,
        detail=(
            f"request_id={promotion.request_id} promoted_at={promotion.promoted_at} "
            f"replayed={promotion.replayed} reason={body.reason}"
        ),
        request=request,
    )
    return compute_epoch_promotion_payload(promotion)


# ---------------------------------------------------------------------------
# Storage asset lifecycle
# ---------------------------------------------------------------------------


async def list_backend_unverified_storage_assets(
    *,
    limit: int,
    cursor: UUID | None,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    """Assets whose backend deletion has not been proven, oldest first."""
    storage_assets = dependencies.storage_assets
    if storage_assets is None:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure storage metering is unavailable",
        )
    try:
        page = await storage_assets.list_backend_unverified(
            limit=limit,
            after_asset_id=cursor,
        )
    except StorageAssetContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        dependencies.logger.exception(
            "Infrastructure backend-unverified asset listing failed"
        )
        raise HTTPException(
            status_code=500,
            detail="Infrastructure backend-unverified asset listing failed",
        ) from exc
    return backend_unverified_storage_payload(page)


async def storage_asset_detail(
    *,
    asset_id: UUID,
    history_limit: int,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    """One asset's incarnations, coverage gaps and destruction assertions."""
    storage_assets = dependencies.storage_assets
    if storage_assets is None:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure storage metering is unavailable",
        )
    try:
        item = await storage_assets.read_asset_detail(
            asset_id=asset_id,
            history_limit=history_limit,
        )
    except StorageAssetNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StorageAssetContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        dependencies.logger.exception("Infrastructure storage asset detail failed")
        raise HTTPException(
            status_code=500,
            detail="Infrastructure storage asset detail failed",
        ) from exc
    return storage_asset_detail_payload(item)


async def assert_storage_asset_destroyed(
    request: Request,
    admin: dict[str, Any],
    asset_id: UUID,
    body: InfrastructureStorageDestructionRequest,
    *,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    """Record operator-attested backend destruction for one storage asset.

    The database constraints are part of the contract: a lifecycle that cannot
    accept the assertion comes back as a 409, not a 500, so a replay or a
    racing collector observation is distinguishable from a server fault.
    """
    storage_assets = dependencies.storage_assets
    if storage_assets is None:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure storage metering is unavailable",
        )
    try:
        result = await storage_assets.assert_destroyed(
            idempotency_key=body.idempotency_key,
            asset_id=asset_id,
            effective_at=body.effective_at,
            evidence_kind=body.evidence_kind,
            evidence_digest=body.evidence_digest,
            actor_kind="user",
            actor_id=UUID(str(admin["id"])),
            reason_code=body.reason_code,
        )
    except StorageAssetNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StorageAssetContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageAssetConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except asyncpg.ForeignKeyViolationError as exc:
        raise HTTPException(status_code=404, detail="Storage asset not found") from exc
    except (
        asyncpg.CheckViolationError,
        asyncpg.ExclusionViolationError,
        asyncpg.ObjectNotInPrerequisiteStateError,
        asyncpg.UniqueViolationError,
    ) as exc:
        raise HTTPException(
            status_code=409,
            detail="Storage destruction assertion conflicts with its lifecycle",
        ) from exc
    except Exception as exc:
        dependencies.logger.exception(
            "Infrastructure storage destruction assertion failed"
        )
        raise HTTPException(
            status_code=500,
            detail="Infrastructure storage destruction assertion failed",
        ) from exc
    await dependencies.audit(
        dependencies.store,
        event_type=(
            "infrastructure_storage_destruction_replayed"
            if result.replayed
            else "infrastructure_storage_destroyed"
        ),
        user=admin,
        resource_type="infrastructure_storage_asset",
        resource_id=str(asset_id),
        detail=(
            f"assertion_id={result.assertion_id} evidence={body.evidence_kind} "
            f"reason_code={body.reason_code} reason={body.reason}"
        ),
        request=request,
    )
    return storage_destruction_payload(result)


# ---------------------------------------------------------------------------
# Cutover, coverage waivers and corrections
# ---------------------------------------------------------------------------


async def metering_cutover_status(
    *, dependencies: InfrastructureAdminDependencies
) -> dict[str, Any]:
    """Where the legacy → typed workspace-metering cutover currently stands."""
    workspace_cutover = dependencies.workspace_cutover
    if workspace_cutover is None:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure metering cutover is unavailable",
        )
    try:
        return cutover_status_payload(await workspace_cutover.status())
    except Exception as exc:
        dependencies.logger.exception("Infrastructure metering cutover status failed")
        raise HTTPException(
            status_code=500,
            detail="Infrastructure metering cutover status failed",
        ) from exc


async def prepare_metering_cutover(
    request: Request,
    admin: dict[str, Any],
    body: InfrastructureCutoverPrepareRequest,
    *,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    """Plan the cutover under a leader fence, refusing an unusable read policy."""
    workspace_cutover = dependencies.workspace_cutover
    if not dependencies.settings.cutover_enabled:
        raise HTTPException(
            status_code=404,
            detail="Infrastructure metering cutover is not enabled",
        )
    if not dependencies.durable_reporting_policy_ready:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure historical reporting policy is unavailable",
        )
    if workspace_cutover is None:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure metering cutover is unavailable",
        )
    generation = dependencies.leader_generation()
    try:
        status = await workspace_cutover.prepare(
            generation,
            actor_id=UUID(str(admin["id"])),
            reason=body.reason,
            idempotency_key=body.idempotency_key,
        )
    except CutoverContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (CutoverBlocked, CutoverConflictError, CutoverFenceError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        dependencies.logger.exception("Infrastructure metering cutover prepare failed")
        raise HTTPException(
            status_code=500,
            detail="Infrastructure metering cutover prepare failed",
        ) from exc
    await dependencies.audit(
        dependencies.store,
        event_type="infrastructure_metering_cutover_prepared",
        user=admin,
        resource_type="infrastructure_metering_cutover",
        resource_id=str(body.idempotency_key),
        detail=f"phase={status.phase.value} cutover_at={status.cutover_at}",
        request=request,
    )
    return cutover_status_payload(status)


async def waive_coverage_gap(
    request: Request,
    admin: dict[str, Any],
    gap_id: UUID,
    body: InfrastructureCoverageWaiverRequest,
    *,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    """Accept a named operator decision to close one metering coverage gap."""
    coverage_waivers = dependencies.coverage_waivers
    if coverage_waivers is None:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure metering coverage waivers are unavailable",
        )
    try:
        result = await coverage_waivers.waive(
            gap_id,
            UUID(str(admin["id"])),
            body.reason,
            body.idempotency_key,
        )
    except CoverageGapNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CoverageGapConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CoverageGapContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        dependencies.logger.exception("Infrastructure metering coverage waiver failed")
        raise HTTPException(
            status_code=500,
            detail="Infrastructure metering coverage waiver failed",
        ) from exc
    await dependencies.audit(
        dependencies.store,
        event_type=(
            "infrastructure_metering_coverage_waiver_replayed"
            if result.replayed
            else "infrastructure_metering_coverage_gap_waived"
        ),
        user=admin,
        resource_type="infrastructure_metering_coverage_gap",
        resource_id=str(result.gap_id),
        detail=(
            f"idempotency_key={result.idempotency_key} "
            f"degraded_days={len(result.degraded_days)}"
        ),
        request=request,
    )
    return {
        "gap_id": result.gap_id,
        "actor_id": result.actor_id,
        "idempotency_key": result.idempotency_key,
        "reason": result.reason,
        "resolved_at": result.resolved_at,
        "replayed": result.replayed,
        "degraded_days": [
            {
                "day": item.day,
                "coverage_sequence": item.coverage_sequence,
                "coverage_revision": item.coverage_revision,
                "unknown_start": item.added_range[0],
                "unknown_end": item.added_range[1],
            }
            for item in result.degraded_days
        ],
    }


async def create_metering_correction(
    request: Request,
    admin: dict[str, Any],
    body: InfrastructureCorrectionRequest,
    *,
    dependencies: InfrastructureAdminDependencies,
) -> dict[str, Any]:
    """Plan an audited correction against already-published metering events."""
    usage_materializer = dependencies.usage_materializer
    if not dependencies.settings.publication_enabled:
        raise HTTPException(
            status_code=404,
            detail="Infrastructure metering publication is not enabled",
        )
    if usage_materializer is None:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure metering correction workflow is unavailable",
        )
    generation = dependencies.leader_generation()
    try:
        plan = await usage_materializer.create_correction(
            generation,
            tuple(
                CorrectionRequestDelta(
                    source=delta.source,
                    source_id=delta.source_id,
                    unit=delta.unit,
                    ts=delta.ts,
                    expected_payload_hash=delta.expected_payload_hash,
                    quantity=delta.quantity,
                    payload_overrides=delta.payload_overrides,
                    inherit_rate=delta.inherit_rate,
                    canonical_rate_version_id=delta.canonical_rate_version_id,
                )
                for delta in body.deltas
            ),
            correction_reason=body.reason,
            correction_actor_id=UUID(str(admin["id"])),
            correction_id=body.idempotency_key,
        )
    except (PublicationContractError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (
        PublicationConflictError,
        PublicationFenceError,
        StrictUsageConflict,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PublicationDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StrictUsageLedgerError as exc:
        raise HTTPException(
            status_code=503,
            detail="Infrastructure metering audit ledger is unavailable",
        ) from exc
    except Exception as exc:
        dependencies.logger.exception(
            "Infrastructure metering correction creation failed"
        )
        raise HTTPException(
            status_code=500,
            detail="Infrastructure metering correction creation failed",
        ) from exc
    await dependencies.audit(
        dependencies.store,
        event_type="infrastructure_metering_correction_reviewed",
        user=admin,
        resource_type="infrastructure_metering_correction",
        resource_id=str(plan.id),
        detail=(
            f"state={plan.state} revision={plan.plan_revision} "
            f"events={len(plan.events)} event_set_hash={plan.event_set_hash}"
        ),
        request=request,
    )
    return {
        "plan_id": plan.id,
        "correction_group_id": plan.correction_group_id,
        "state": plan.state,
        "plan_revision": plan.plan_revision,
        "period_start": plan.period_start,
        "period_end": plan.period_end,
        "event_count": len(plan.events),
        "event_set_hash": plan.event_set_hash,
        "rate_selection_hash": plan.rate_selection_hash,
    }
