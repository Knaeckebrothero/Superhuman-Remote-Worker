"""Infrastructure-metering activation policy as pure, parameterized decisions.

Every function here is a decision over explicit inputs — settings, schema
capabilities, and the irreversible activation rows the database owns. Nothing
in this module reads an application global, opens a connection, or depends on
startup order, so ``lifespan`` and the admin routes can ask the same question
and get the same answer without one of them being the authority for the other.

The distinction the module exists to keep straight:

* **effective** — the class may be *published to right now*. Requires the
  one-way state, its activation boundary, and a database clock that has passed
  it. Reversible: turning a Helm boolean off stops publication.
* **durable** — the class *was* activated, so history exists for it. Not
  reversible, and therefore not gated on the current Helm booleans: losing a
  boolean must never make already-published history disappear from a typed read
  or from a day-sealing completeness decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Literal, Protocol

from orchestrator.services.infrastructure_metering import (
    InfrastructureMeteringSettings,
    MeteringSchemaCapabilities,
)
from orchestrator.services.infrastructure_metering.compute_activation import (
    ComputeActivation,
)
from orchestrator.services.infrastructure_metering.materializer import (
    StoragePublicationAuthority,
    StoragePublicationPolicy,
)
from orchestrator.services.infrastructure_metering.storage_assets import (
    StorageActivation,
    StorageSourceActivation,
    StorageSourceRequirementSpec,
)

INFRASTRUCTURE_PVC_RESOURCES = (
    "workspace_pvc",
    "session_workspace_pvc",
    "session_agent_pvc",
    "persistent_agent_pvc",
    "vm_rootdisk_claim",
    "golden_image_pvc",
    "platform_pvc",
    "unclassified_pvc",
)
INFRASTRUCTURE_PV_RESOURCES = ("unmapped_block_volume",)

StorageSource = Literal["primary", "vm"]
MeasurementBasis = Literal["claim-requested", "volume-provisioned"]


class WorkspaceOwnerStore(Protocol):
    """The two owner reads workspace metering attributes a compute row through."""

    async def get_job(self, job_id: str) -> Any: ...

    async def get_thread(self, thread_id: str) -> Any: ...


def enabled_infrastructure_publication_resources(
    settings: InfrastructureMeteringSettings,
    *,
    mapped_volume_resources: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return exact class gates without broadening the workspace default."""

    resources = ["workspace_pod"]
    if settings.agent_pod_publication_enabled:
        resources.append("agent_pod")
    if settings.vm_publication_enabled:
        resources.append("workspace_vm")
    if settings.pvc_publication_enabled or settings.vm_pvc_publication_enabled:
        resources.extend(INFRASTRUCTURE_PVC_RESOURCES)
    if settings.pv_publication_enabled or settings.vm_pv_publication_enabled:
        resources.extend((*INFRASTRUCTURE_PV_RESOURCES, *mapped_volume_resources))
    return tuple(dict.fromkeys(resources))


def storage_activation_is_effective(activation: StorageActivation | None) -> bool:
    """Require both the one-way state and its database-owned UTC boundary."""

    return bool(
        activation is not None
        and activation.state == "active"
        and activation.activated_at is not None
        and activation.database_time is not None
        and activation.database_time >= activation.activated_at
    )


def storage_source_activation_is_effective(
    activation: StorageSourceActivation | None,
    global_activation: StorageActivation | None,
) -> bool:
    """Require both the global master and exact source boundary to be effective."""

    return bool(
        activation is not None
        and global_activation is not None
        and activation.state == "active"
        and global_activation.state == "active"
        and activation.activated_at is not None
        and global_activation.activated_at is not None
        and activation.database_time is not None
        and global_activation.database_time is not None
        and activation.database_time
        >= max(activation.activated_at, global_activation.activated_at)
        and global_activation.database_time >= global_activation.activated_at
    )


def storage_activation_is_durable(activation: StorageActivation | None) -> bool:
    """Return whether a one-way storage activation has been scheduled."""

    return bool(
        activation is not None
        and activation.state == "active"
        and activation.activated_at is not None
    )


def storage_source_activation_is_durable(
    activation: StorageSourceActivation | None,
    global_activation: StorageActivation | None,
) -> bool:
    """Return whether an exact source and its global basis are one-way active."""

    return bool(
        activation is not None
        and storage_activation_is_durable(global_activation)
        and activation.state == "active"
        and activation.activated_at is not None
    )


def compute_activation_is_durable(activation: ComputeActivation | None) -> bool:
    """Return whether a compute class crossed the one-way scheduling step."""

    return bool(
        activation is not None
        and activation.state == "active"
        and activation.activated_at is not None
    )


def storage_source_configuration(
    source: StorageSource,
    measurement_basis: MeasurementBasis,
    settings: InfrastructureMeteringSettings,
) -> tuple[str, str, tuple[StorageSourceRequirementSpec, ...]]:
    """Resolve one operator-facing source to its immutable inventory contract."""

    if source == "primary":
        collector_id = "kubernetes-pods"
        source_cluster = settings.stable_cluster_id
        namespaces = settings.namespace_allowlist
    else:
        collector_id = "kubevirt-storage"
        source_cluster = settings.vm_stable_cluster_id
        namespaces = (settings.vm_namespace,)

    requirements: list[StorageSourceRequirementSpec] = []
    if measurement_basis == "claim-requested":
        requirements.extend(
            StorageSourceRequirementSpec(
                api_resource="core/v1/persistentvolumeclaims",
                namespace=namespace,
                requirement_role="quantity",
            )
            for namespace in namespaces
        )
    else:
        requirements.append(
            StorageSourceRequirementSpec(
                api_resource="core/v1/persistentvolumes",
                namespace=None,
                requirement_role="quantity",
            )
        )
        requirements.extend(
            StorageSourceRequirementSpec(
                api_resource="core/v1/persistentvolumeclaims",
                namespace=namespace,
                requirement_role="attribution",
            )
            for namespace in namespaces
        )
    return collector_id, source_cluster, tuple(requirements)


def compute_scope_configuration(
    activation_key: str,
    settings: InfrastructureMeteringSettings,
) -> tuple[str, tuple[str, ...], str]:
    """Resolve one compute class to the exact scope its collector must cover.

    Shared with ``lifespan``: the boot-time scope diagnostic and the admin
    activation routes have to agree on this triple, or a class boots reported
    as scope-incompatible while its own route happily schedules it.
    """

    if activation_key == "workspace_vm":
        return (
            settings.vm_stable_cluster_id,
            (settings.vm_namespace,),
            "kubevirt-vmis",
        )
    return (
        settings.stable_cluster_id,
        settings.namespace_allowlist,
        "kubernetes-pods",
    )


def storage_source_inventory_enabled(
    source: StorageSource,
    measurement_basis: MeasurementBasis,
    settings: InfrastructureMeteringSettings,
) -> bool:
    if source == "primary":
        claim_enabled = settings.pvc_inventory_enabled
        volume_enabled = settings.pv_inventory_enabled
    else:
        claim_enabled = settings.vm_pvc_inventory_enabled
        volume_enabled = settings.vm_pv_inventory_enabled
    return (
        claim_enabled
        if measurement_basis == "claim-requested"
        else (claim_enabled and volume_enabled)
    )


def storage_source_shadow_enabled(
    source: StorageSource,
    measurement_basis: MeasurementBasis,
    settings: InfrastructureMeteringSettings,
) -> bool:
    if source == "primary":
        claim_enabled = settings.pvc_shadow_enabled
        volume_enabled = settings.pv_shadow_enabled
    else:
        claim_enabled = settings.vm_pvc_shadow_enabled
        volume_enabled = settings.vm_pv_shadow_enabled
    return (
        claim_enabled
        if measurement_basis == "claim-requested"
        else (claim_enabled and volume_enabled)
    )


def storage_source_publication_enabled(
    source: StorageSource,
    measurement_basis: MeasurementBasis,
    settings: InfrastructureMeteringSettings,
) -> bool:
    if source == "primary":
        return (
            settings.pvc_publication_enabled
            if measurement_basis == "claim-requested"
            else settings.pv_publication_enabled
        )
    return (
        settings.vm_pvc_publication_enabled
        if measurement_basis == "claim-requested"
        else settings.vm_pv_publication_enabled
    )


def storage_source_shadow_requested(
    settings: InfrastructureMeteringSettings,
    source: StorageSource,
    measurement_basis: MeasurementBasis,
) -> bool:
    if source == "primary":
        return (
            settings.pvc_shadow_enabled
            if measurement_basis == "claim-requested"
            else settings.pv_shadow_enabled
        )
    return (
        settings.vm_pvc_shadow_enabled
        if measurement_basis == "claim-requested"
        else settings.vm_pv_shadow_enabled
    )


def storage_source_configuration_errors(
    settings: InfrastructureMeteringSettings,
    activations: tuple[StorageSourceActivation, ...],
) -> tuple[str, ...]:
    """Reject configured shadow writers that outgrow their frozen scope set."""

    by_identity = {
        (
            activation.measurement_basis,
            activation.collector_id,
            activation.source_cluster,
        ): activation
        for activation in activations
    }
    errors: list[str] = []
    for source in ("primary", "vm"):
        for basis in ("claim-requested", "volume-provisioned"):
            if not storage_source_shadow_requested(settings, source, basis):
                continue
            collector_id, source_cluster, configured = storage_source_configuration(
                source, basis, settings
            )
            activation = by_identity.get((basis, collector_id, source_cluster))
            label = f"{source}/{basis}"
            if activation is None or activation.state == "disabled":
                errors.append(f"{label} durable source shadow activation")
                continue
            configured_identities = {
                (
                    requirement.api_resource,
                    requirement.namespace,
                    requirement.requirement_role,
                )
                for requirement in configured
            }
            frozen_identities = {
                (
                    requirement.api_resource,
                    requirement.namespace,
                    requirement.requirement_role,
                )
                for requirement in activation.requirements
            }
            if configured_identities != frozen_identities:
                errors.append(f"{label} frozen inventory scope set")
    return tuple(errors)


def durable_storage_reporting_policy(
    *,
    claim_activation: StorageActivation | None,
    volume_activation: StorageActivation | None,
    source_activations: tuple[StorageSourceActivation, ...],
) -> StoragePublicationPolicy:
    """Build the read/seal source set from irreversible database activation.

    Current Helm publication booleans and VM lifecycle credentials remain
    write-safety inputs. Losing either must not make already-published history
    disappear from typed reads or from a day-sealing completeness decision.
    """

    global_by_basis = {
        "claim-requested": claim_activation,
        "volume-provisioned": volume_activation,
    }
    return StoragePublicationPolicy(
        tuple(
            StoragePublicationAuthority(
                measurement_basis=activation.measurement_basis,
                collector_id=activation.collector_id,
                source_cluster=activation.source_cluster,
            )
            for activation in source_activations
            if storage_source_activation_is_durable(
                activation,
                global_by_basis.get(activation.measurement_basis),
            )
        )
    )


def durable_infrastructure_reporting_resources(
    capabilities: MeteringSchemaCapabilities,
    *,
    mapped_volume_resources: tuple[str, ...] = (),
    volume_mapping_ready: bool = True,
    compute_activations: Mapping[str, ComputeActivation] | None = None,
    storage_reporting_policy: StoragePublicationPolicy | None = None,
) -> tuple[str, ...]:
    """Build the historical read/seal class set from durable activation.

    Unlike publication, this policy is not reversible. If a durable class
    exists but its schema or immutable volume mapping registry is unavailable,
    fail wiring closed instead of returning an allowlist that silently omits
    that history.
    """

    resources = ["workspace_pod"]
    activations = compute_activations or {}
    durable_compute = {
        key
        for key, activation in activations.items()
        if compute_activation_is_durable(activation)
    }
    if durable_compute and not capabilities.slice3_compute_inventory_ready:
        raise ValueError("durable compute reporting schema is unavailable")
    if "agent_pod" in durable_compute:
        resources.append("agent_pod")
    if "workspace_vm" in durable_compute:
        resources.append("workspace_vm")

    storage_policy = storage_reporting_policy or StoragePublicationPolicy()
    durable_claim = any(
        authority.measurement_basis == "claim-requested"
        for authority in storage_policy.authorities
    )
    durable_volume = any(
        authority.measurement_basis == "volume-provisioned"
        for authority in storage_policy.authorities
    )
    if (
        durable_claim or durable_volume
    ) and not capabilities.slice3_storage_lifecycle_ready:
        raise ValueError("durable storage reporting schema is unavailable")
    if durable_claim:
        resources.extend(INFRASTRUCTURE_PVC_RESOURCES)
    if durable_volume:
        if not capabilities.slice2_volume_inventory_ready:
            raise ValueError("durable volume reporting schema is unavailable")
        if not volume_mapping_ready:
            raise ValueError("durable volume mapping registry is unavailable")
        resources.extend((*INFRASTRUCTURE_PV_RESOURCES, *mapped_volume_resources))
    return tuple(dict.fromkeys(resources))


def durable_collection_settings(
    settings: InfrastructureMeteringSettings,
    *,
    compute_activations: Mapping[str, ComputeActivation] | None = None,
    claim_activation: StorageActivation | None = None,
    volume_activation: StorageActivation | None = None,
    source_activations: tuple[StorageSourceActivation, ...] = (),
) -> InfrastructureMeteringSettings:
    """Keep active-class mutation routes armed while their source is enabled.

    Helm inventory booleans still control whether the source exists. If an
    operator turns off only a shadow boolean for a database-active class, the
    server keeps requiring shadow ingestion. A stale collector then gets a
    visible mode mismatch and coverage freezes instead of accepting healthy
    inventory that silently omits specialized intervals.
    """

    activations = compute_activations or {}
    force_agent = bool(
        settings.collector_enabled
        and compute_activation_is_durable(activations.get("agent_pod"))
    )
    force_ide = bool(
        settings.collector_enabled
        and compute_activation_is_durable(activations.get("ide_workspace_pod"))
    )
    force_vm = bool(
        settings.vm_inventory_enabled
        and compute_activation_is_durable(activations.get("workspace_vm"))
    )

    global_by_basis = {
        "claim-requested": claim_activation,
        "volume-provisioned": volume_activation,
    }
    durable_storage = {
        (
            activation.measurement_basis,
            activation.collector_id,
            activation.source_cluster,
        )
        for activation in source_activations
        if storage_source_activation_is_durable(
            activation,
            global_by_basis.get(activation.measurement_basis),
        )
    }
    force_primary_claim = bool(
        settings.pvc_inventory_enabled
        and (
            "claim-requested",
            "kubernetes-pods",
            settings.stable_cluster_id,
        )
        in durable_storage
    )
    force_primary_volume = bool(
        settings.pv_inventory_enabled
        and (
            "volume-provisioned",
            "kubernetes-pods",
            settings.stable_cluster_id,
        )
        in durable_storage
    )
    force_vm_claim = bool(
        settings.vm_pvc_inventory_enabled
        and (
            "claim-requested",
            "kubevirt-storage",
            settings.vm_stable_cluster_id,
        )
        in durable_storage
    )
    force_vm_volume = bool(
        settings.vm_pv_inventory_enabled
        and (
            "volume-provisioned",
            "kubevirt-storage",
            settings.vm_stable_cluster_id,
        )
        in durable_storage
    )
    forced = any(
        (
            force_agent,
            force_ide,
            force_vm,
            force_primary_claim,
            force_primary_volume,
            force_vm_claim,
            force_vm_volume,
        )
    )
    if not forced:
        return settings
    return replace(
        settings,
        shadow_enabled=(settings.shadow_enabled or force_agent or force_ide),
        agent_pod_shadow_enabled=settings.agent_pod_shadow_enabled or force_agent,
        ide_pod_shadow_enabled=settings.ide_pod_shadow_enabled or force_ide,
        vm_shadow_enabled=settings.vm_shadow_enabled or force_vm,
        pvc_shadow_enabled=settings.pvc_shadow_enabled or force_primary_claim,
        pv_shadow_enabled=settings.pv_shadow_enabled or force_primary_volume,
        vm_pvc_shadow_enabled=settings.vm_pvc_shadow_enabled or force_vm_claim,
        vm_pv_shadow_enabled=settings.vm_pv_shadow_enabled or force_vm_volume,
    )


def requested_storage_publication_policy(
    settings: InfrastructureMeteringSettings,
    *,
    vm_lifecycle_authenticated: bool = False,
) -> StoragePublicationPolicy:
    """Build the fixed-per-process exact source allowlist from independent gates."""

    authorities: list[StoragePublicationAuthority] = []
    for enabled, measurement_basis, collector_id, source_cluster in (
        (
            settings.pvc_publication_enabled,
            "claim-requested",
            "kubernetes-pods",
            settings.stable_cluster_id,
        ),
        (
            settings.pv_publication_enabled,
            "volume-provisioned",
            "kubernetes-pods",
            settings.stable_cluster_id,
        ),
        (
            settings.vm_pvc_publication_enabled,
            "claim-requested",
            "kubevirt-storage",
            settings.vm_stable_cluster_id,
        ),
        (
            settings.vm_pv_publication_enabled,
            "volume-provisioned",
            "kubevirt-storage",
            settings.vm_stable_cluster_id,
        ),
    ):
        is_vm_authority = collector_id == "kubevirt-storage"
        if enabled and (not is_vm_authority or vm_lifecycle_authenticated):
            authorities.append(
                StoragePublicationAuthority(
                    measurement_basis=measurement_basis,
                    collector_id=collector_id,
                    source_cluster=source_cluster,
                )
            )
    return StoragePublicationPolicy(tuple(authorities))


def capability_gated_storage_publication_policy(
    requested: StoragePublicationPolicy,
    capabilities: MeteringSchemaCapabilities,
    *,
    claim_activation: StorageActivation | None,
    volume_activation: StorageActivation | None,
    source_activations: Mapping[tuple[str, str, str], StorageSourceActivation],
    volume_mapping_ready: bool,
    volume_identity_key_matches: bool,
) -> StoragePublicationPolicy:
    """Remove any source whose schema, identity, or boundary is not ready."""

    if not capabilities.slice3_storage_lifecycle_ready:
        return StoragePublicationPolicy()
    enabled: list[StoragePublicationAuthority] = []
    for authority in requested.authorities:
        global_activation = (
            claim_activation
            if authority.measurement_basis == "claim-requested"
            else volume_activation
        )
        source_activation = source_activations.get(
            (
                authority.measurement_basis,
                authority.collector_id,
                authority.source_cluster,
            )
        )
        if not storage_source_activation_is_effective(
            source_activation,
            global_activation,
        ):
            continue
        if authority.measurement_basis == "volume-provisioned" and (
            not volume_mapping_ready or not volume_identity_key_matches
        ):
            continue
        enabled.append(authority)
    return StoragePublicationPolicy(tuple(enabled))


def compute_activation_is_effective(
    activation: ComputeActivation | None,
) -> bool:
    return bool(
        activation is not None
        and activation.state == "active"
        and activation.activated_at is not None
        and activation.database_time is not None
        and activation.database_time >= activation.activated_at
    )


def capability_gated_infrastructure_publication_resources(
    settings: InfrastructureMeteringSettings,
    capabilities: MeteringSchemaCapabilities,
    *,
    mapped_volume_resources: tuple[str, ...] = (),
    volume_mapping_ready: bool = True,
    compute_activations: Mapping[str, ComputeActivation] | None = None,
    storage_publication_policy: StoragePublicationPolicy | None = None,
    vm_lifecycle_authenticated: bool = False,
) -> tuple[str, ...]:
    """Narrow requested classes to schema- and activation-safe resources."""

    resources = ["workspace_pod"]
    activations = compute_activations or {}
    storage_policy = storage_publication_policy or StoragePublicationPolicy()
    if (
        settings.agent_pod_publication_enabled
        and capabilities.slice3_compute_inventory_ready
        and compute_activation_is_effective(activations.get("agent_pod"))
    ):
        resources.append("agent_pod")
    if (
        settings.vm_publication_enabled
        and vm_lifecycle_authenticated
        and capabilities.slice3_compute_inventory_ready
        and compute_activation_is_effective(activations.get("workspace_vm"))
    ):
        resources.append("workspace_vm")
    if (
        any(
            authority.measurement_basis == "claim-requested"
            for authority in storage_policy.authorities
        )
        and capabilities.slice3_storage_lifecycle_ready
    ):
        resources.extend(INFRASTRUCTURE_PVC_RESOURCES)
    if (
        any(
            authority.measurement_basis == "volume-provisioned"
            for authority in storage_policy.authorities
        )
        and volume_mapping_ready
        and capabilities.slice2_volume_inventory_ready
        and capabilities.storage_identity_key_version
        == settings.volume_identity_key_version
        and capabilities.slice3_storage_lifecycle_ready
    ):
        resources.extend((*INFRASTRUCTURE_PV_RESOURCES, *mapped_volume_resources))
    return tuple(dict.fromkeys(resources))


async def workspace_metering_attribution(
    owner_kind: str, owner_id: str, *, store: WorkspaceOwnerStore
) -> dict[str, Any] | None:
    """Resolve a workspace owner → {user_id, project_id} for ledger attribution.

    Best-effort (used by the Slice 4b metering loop): a missing/deleted owner
    yields None — the compute row is still recorded, just unattributed.
    """
    try:
        row = (
            await store.get_job(owner_id)
            if owner_kind == "job"
            else await store.get_thread(owner_id)
        )
        if not row:
            return None
        return {"user_id": row.get("user_id"), "project_id": row.get("project_id")}
    except Exception:
        return None
