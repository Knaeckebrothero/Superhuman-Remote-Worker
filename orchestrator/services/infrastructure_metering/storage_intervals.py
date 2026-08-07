"""PVC demand and durable-volume interval reconciliation for Slice 2.

This module consumes only the strict, raw-free PVC/PV payloads accepted by the
inventory ingestion boundary.  It keeps logical claims and physical assets in
separate measurement bases, records immutable storage shadow observations, and
opens resource intervals only after the database-owned storage activation
boundary has passed.

Kubernetes labels are attribution hints, never authority.  Customer ownership
requires a canonical full UUID label, the exact deterministic PVC name emitted
by the relevant provisioner, and a live app-database job/thread row.  A bound PV
inherits attribution only from the current validated claim interval.  Retained
assets use the physical-asset registry and freeze confirmed accrual when their
PV incarnation disappears without backend proof.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import re
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

import asyncpg

from .inventory import (
    InventoryConflictError,
    InventoryContractError,
    InventoryItem,
    SnapshotAbsenceMutationContext,
    SnapshotCompletionContext,
    SnapshotIntervalMutationContext,
    SnapshotObservationContext,
    WatchDeletionMutationContext,
    WatchIntervalMutationContext,
    WatchMutationAction,
)
from .storage_assets import (
    StorageActivationNotReady,
    VolumeAssetRecord,
    derive_volume_asset_identity,
    ensure_volume_asset,
    lock_storage_activation,
    observe_volume_incarnation,
    open_backend_unverified_gap,
    read_storage_activation,
    register_storage_identity_key,
    resolve_backend_gap_reobserved,
)
from .storage_mapping import (
    StorageResourceMappingKey,
    resolve_storage_resource_mapping,
)


_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PVC_LIFECYCLE_NAMESPACE = UUID("84c4b692-d9f8-5f93-a8ee-43b18def8998")
_BATCH_SIZE = 500

_PVC_SHAPES = (
    # Dynamic product identities are intentionally checked before generic Helm
    # labels.  Product PVCs often carry both.
    (
        "srw-workspace",
        "workspace-pvc",
        "srw/job-id",
        "job",
        "pvc-workspace",
        "workspace_pvc",
        "workspace-job-claim",
    ),
    (
        "srw-workspace",
        "workspace-pvc",
        "srw/thread-id",
        "thread",
        "pvc-ws-thread",
        "session_workspace_pvc",
        "workspace-session-claim",
    ),
    (
        "srw-agent",
        "agent-workspace-pvc",
        "srw/thread-id",
        "thread",
        "pvc-agent-s",
        "session_agent_pvc",
        "agent-session-claim",
    ),
    (
        "srw-persistent-agent",
        "agent-workspace-pvc",
        "srw/thread-id",
        "thread",
        "pvc-persistent",
        "persistent_agent_pvc",
        "persistent-agent-claim",
    ),
)

_VM_ROOTDISK_PREFIX = "agent-vm-"
_VM_ROOTDISK_SUFFIX = "-rootdisk"
_VM_OWNER_ALIAS_KINDS: Mapping[str, Literal["job", "thread"] | None] = {
    # The VM controller historically used ``job-id`` for both jobs and
    # threads, so it proves only the identifier, not the owner kind.
    "job-id": None,
    "thread-id": "thread",
    "srw/job-id": "job",
    "srw/thread-id": "thread",
    "srw.io/job-id": "job",
    "srw.io/thread-id": "thread",
}


@dataclass(frozen=True, slots=True)
class _OwnerHint:
    kind: Literal["job", "thread"]
    id: UUID


@dataclass(frozen=True, slots=True)
class StorageAttribution:
    scope: Literal["customer", "shared-platform", "unknown"]
    owner_kind: Literal["job", "thread", "platform"] | None
    owner_id: UUID | None
    user_id: UUID | None
    project_id: UUID | None
    source: str
    quality: Literal["exact", "derived", "ambiguous", "unknown"]
    reason_code: str


@dataclass(frozen=True, slots=True)
class StorageProjection:
    source_kind: Literal["pvc", "volume"]
    source_uid: str
    api_version: str | None
    resource_version: str | None
    namespace: str | None
    name: str | None
    measurement_basis: Literal["claim-requested", "volume-provisioned"]
    resource_class: Literal["persistent-volume-claim", "persistent-volume"]
    cost_domain: Literal["workload-allocation", "physical-asset"]
    resource: str
    mapping_version: str | None
    mapping_fingerprint: str | None
    storage_bytes: int | None
    capacity_source: str | None
    measurement_algorithm: str | None
    creation_timestamp: datetime | None
    deletion_requested: bool
    storage_class: str | None
    access_modes: tuple[str, ...]
    volume_mode: str | None
    bound_volume_name: str | None
    reclaim_policy: str
    backend_deletion_finalizer_observed: bool
    claim_reference: tuple[str, str, str] | None
    identity_scheme: str | None
    identity_key_version: str | None
    identity_key_fingerprint: str | None
    pv_uid: str | None
    csi_driver: str | None
    owner_hint: _OwnerHint | None
    static_attribution: StorageAttribution | None
    classification_reason: str
    attribution_ambiguous: bool
    valid_for_interval: bool


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _optional_text(value: Any, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        return None
    return value


def _canonical_uuid(value: Any) -> UUID | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value else None


def _uuid_value(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _unknown(reason: str) -> StorageAttribution:
    return StorageAttribution(
        scope="unknown",
        owner_kind=None,
        owner_id=None,
        user_id=None,
        project_id=None,
        source="kubernetes-identity-unresolved",
        quality="ambiguous",
        reason_code=reason,
    )


def _shared(reason: str) -> StorageAttribution:
    return StorageAttribution(
        scope="shared-platform",
        owner_kind="platform",
        owner_id=None,
        user_id=None,
        project_id=None,
        source="kubernetes-platform-classification",
        quality="derived",
        reason_code=reason,
    )


def _fallback_projection(item: InventoryItem) -> StorageProjection:
    source_kind: Literal["pvc", "volume"]
    if item.source_kind == "pvc":
        source_kind = "pvc"
        basis: Literal["claim-requested", "volume-provisioned"] = "claim-requested"
        resource_class: Literal["persistent-volume-claim", "persistent-volume"] = (
            "persistent-volume-claim"
        )
        domain: Literal["workload-allocation", "physical-asset"] = "workload-allocation"
        resource = "unclassified_pvc"
    elif item.source_kind == "volume":
        source_kind = "volume"
        basis = "volume-provisioned"
        resource_class = "persistent-volume"
        domain = "physical-asset"
        resource = "unmapped_block_volume"
    else:
        raise InventoryContractError("storage reconciler received another source kind")
    reason = item.item_error.code if item.item_error is not None else "invalid-item"
    if not _SAFE_CODE.fullmatch(reason):
        reason = "invalid-item"
    namespace = item.normalized_item.get("namespace")
    return StorageProjection(
        source_kind=source_kind,
        source_uid=item.source_uid,
        api_version=None,
        resource_version=None,
        namespace=namespace if isinstance(namespace, str) else None,
        name=None,
        measurement_basis=basis,
        resource_class=resource_class,
        cost_domain=domain,
        resource=resource,
        mapping_version=None,
        mapping_fingerprint=None,
        storage_bytes=None,
        capacity_source=None,
        measurement_algorithm=None,
        creation_timestamp=None,
        deletion_requested=False,
        storage_class=None,
        access_modes=(),
        volume_mode=None,
        bound_volume_name=None,
        reclaim_policy="unknown",
        backend_deletion_finalizer_observed=False,
        claim_reference=None,
        identity_scheme=None,
        identity_key_version=None,
        identity_key_fingerprint=None,
        pv_uid=None,
        csi_driver=None,
        owner_hint=None,
        static_attribution=None,
        classification_reason=reason,
        attribution_ambiguous=True,
        valid_for_interval=False,
    )


def _classify_pvc(
    *, labels: Mapping[str, Any], name: str
) -> tuple[str, _OwnerHint | None, StorageAttribution | None, str, bool]:
    job_label = labels.get("srw/job-id")
    thread_label = labels.get("srw/thread-id")
    matched_product_shapes: list[tuple[str, str]] = []

    # A DataVolume is only the provisioning link that created this PVC.  The
    # claim itself is the single storage asset and carries the explicit owner
    # hint propagated by CDI.  Labels remain untrusted until the app-DB VM
    # context is checked by _resolve_vm_rootdisk_owner().
    rootdisk_marker = labels.get("srw.io/rootdisk")
    if rootdisk_marker == "true" or (
        name.startswith(_VM_ROOTDISK_PREFIX) and name.endswith(_VM_ROOTDISK_SUFFIX)
    ):
        if (
            rootdisk_marker not in {None, "true"}
            or labels.get("srw.io/golden-image")
            or labels.get("srw.io/vm-image")
        ):
            return (
                "vm_rootdisk_claim",
                None,
                _unknown("vm-rootdisk-classification-conflict"),
                "vm-rootdisk-classification-conflict",
                True,
            )
        owner_kind = labels.get("srw.io/owner-kind")
        raw_owner_id = labels.get("srw.io/owner-id")
        if owner_kind is None or raw_owner_id is None:
            return (
                "vm_rootdisk_claim",
                None,
                _unknown("vm-owner-hint-missing"),
                "vm-owner-hint-missing",
                True,
            )
        owner_id = _canonical_uuid(raw_owner_id)
        if owner_kind not in {"job", "thread"} or owner_id is None:
            return (
                "vm_rootdisk_claim",
                None,
                _unknown("vm-owner-hint-invalid"),
                "vm-owner-hint-invalid",
                True,
            )
        expected_name = f"{_VM_ROOTDISK_PREFIX}{owner_id}{_VM_ROOTDISK_SUFFIX}"
        if name != expected_name:
            return (
                "vm_rootdisk_claim",
                None,
                _unknown("vm-rootdisk-name-mismatch"),
                "vm-rootdisk-name-mismatch",
                True,
            )
        for alias, alias_kind in _VM_OWNER_ALIAS_KINDS.items():
            alias_owner = labels.get(alias)
            if alias_owner is None:
                continue
            if alias_owner != str(owner_id) or (
                alias_kind is not None and alias_kind != owner_kind
            ):
                return (
                    "vm_rootdisk_claim",
                    None,
                    _unknown("vm-owner-hint-conflict"),
                    "vm-owner-hint-conflict",
                    True,
                )
        return (
            "vm_rootdisk_claim",
            _OwnerHint(kind=owner_kind, id=owner_id),  # type: ignore[arg-type]
            None,
            "vm-rootdisk-owner-hint",
            False,
        )

    if labels.get("srw.io/golden-image") or labels.get("srw.io/vm-image"):
        return (
            "golden_image_pvc",
            None,
            _shared("golden-image-claim"),
            "golden-image-claim",
            False,
        )

    for (
        app,
        component,
        owner_label,
        owner_kind,
        name_prefix,
        resource,
        reason,
    ) in _PVC_SHAPES:
        if labels.get("app") != app or labels.get("srw/component") != component:
            continue
        matched_product_shapes.append((name_prefix, resource))
        if job_label and thread_label:
            return (
                resource,
                None,
                _unknown("conflicting-owner-labels"),
                "conflicting-owner-labels",
                True,
            )
        raw_owner = labels.get(owner_label)
        # Workspace and session workspace claims intentionally share their
        # app/component labels.  Absence of this candidate's owner key is not
        # yet invalid: the next exact shape may carry the other owner key.
        if raw_owner is None:
            continue
        owner_id = _canonical_uuid(raw_owner)
        if owner_id is None:
            return (
                resource,
                None,
                _unknown("invalid-owner-label"),
                "invalid-owner-label",
                True,
            )
        expected_name = f"{name_prefix}-{str(owner_id)[:12]}"
        if name != expected_name:
            return (
                resource,
                None,
                _unknown("owner-name-mismatch"),
                "owner-name-mismatch",
                True,
            )
        return (
            resource,
            _OwnerHint(kind=owner_kind, id=owner_id),  # type: ignore[arg-type]
            None,
            reason,
            False,
        )

    if matched_product_shapes:
        resource = next(
            (
                candidate_resource
                for name_prefix, candidate_resource in matched_product_shapes
                if name.startswith(f"{name_prefix}-")
            ),
            matched_product_shapes[0][1],
        )
        return (
            resource,
            None,
            _unknown("invalid-owner-label"),
            "invalid-owner-label",
            True,
        )

    if labels.get("app.kubernetes.io/managed-by") == "Helm":
        return (
            "platform_pvc",
            None,
            _shared("helm-platform-claim"),
            "helm-platform-claim",
            False,
        )
    return (
        "unclassified_pvc",
        None,
        _unknown("unclassified-pvc"),
        "unclassified-pvc",
        True,
    )


def project_storage_item(item: InventoryItem) -> StorageProjection:
    """Project one strict normalized item into storage-metering dimensions."""

    if not item.valid_for_metering:
        return _fallback_projection(item)
    if item.source_kind not in {"pvc", "volume"}:
        raise InventoryContractError("storage reconciler received another source kind")
    payload = _mapping(item.normalized_item)
    api_version = _optional_text(payload.get("api_version"), maximum=64)
    resource_version = payload.get("resource_version")
    if resource_version is not None and not isinstance(resource_version, str):
        resource_version = None
    name = _optional_text(payload.get("name"), maximum=253)
    namespace_value = payload.get("namespace")
    namespace = namespace_value if isinstance(namespace_value, str) else None
    lifecycle = _mapping(payload.get("lifecycle"))
    capacity = _mapping(payload.get("capacity"))
    storage_bytes = _nonnegative_int(capacity.get("storage_bytes"))
    capacity_source = _optional_text(capacity.get("source"), maximum=64)
    algorithm = _optional_text(
        capacity.get("measurement_algorithm") or payload.get("measurement_algorithm"),
        maximum=128,
    )
    if api_version is None or name is None:
        raise InventoryContractError("normalized storage identity is incomplete")
    access_modes_value = payload.get("access_modes")
    access_modes = (
        tuple(value for value in access_modes_value if isinstance(value, str))
        if isinstance(access_modes_value, list)
        else ()
    )
    storage_class = _optional_text(payload.get("storage_class"), maximum=253)
    volume_mode = _optional_text(payload.get("volume_mode"), maximum=64)
    creation = _timestamp(lifecycle.get("creation_timestamp"))
    valid_for_interval = (
        storage_bytes is not None
        and capacity_source is not None
        and algorithm is not None
        and item.revision_hash is not None
        and _HASH.fullmatch(item.revision_hash) is not None
    )

    if item.source_kind == "pvc":
        if namespace is None:
            raise InventoryContractError("PVC projection lacks namespace")
        labels = _mapping(payload.get("labels"))
        resource, hint, static, reason, ambiguous = _classify_pvc(
            labels=labels, name=name
        )
        return StorageProjection(
            source_kind="pvc",
            source_uid=item.source_uid,
            api_version=api_version,
            resource_version=resource_version,
            namespace=namespace,
            name=name,
            measurement_basis="claim-requested",
            resource_class="persistent-volume-claim",
            cost_domain="workload-allocation",
            resource=resource,
            mapping_version=None,
            mapping_fingerprint=None,
            storage_bytes=storage_bytes,
            capacity_source=capacity_source,
            measurement_algorithm=algorithm,
            creation_timestamp=creation,
            deletion_requested=lifecycle.get("deletion_requested") is True,
            storage_class=storage_class,
            access_modes=access_modes,
            volume_mode=volume_mode,
            bound_volume_name=_optional_text(
                payload.get("bound_volume_name"), maximum=253
            ),
            reclaim_policy="unknown",
            backend_deletion_finalizer_observed=False,
            claim_reference=None,
            identity_scheme=None,
            identity_key_version=None,
            identity_key_fingerprint=None,
            pv_uid=None,
            csi_driver=None,
            owner_hint=hint,
            static_attribution=static,
            classification_reason=reason,
            attribution_ambiguous=ambiguous,
            valid_for_interval=valid_for_interval,
        )

    if namespace is not None:
        raise InventoryContractError("PV projection unexpectedly has namespace")
    identity = _mapping(payload.get("volume_identity"))
    claim = _mapping(payload.get("claim_reference"))
    claim_reference = None
    if claim:
        claim_uid = _optional_text(claim.get("uid"), maximum=256)
        claim_namespace = _optional_text(claim.get("namespace"), maximum=253)
        claim_name = _optional_text(claim.get("name"), maximum=253)
        if claim_uid and claim_namespace and claim_name:
            claim_reference = (claim_uid, claim_namespace, claim_name)
    identity_scheme = _optional_text(identity.get("scheme"), maximum=64)
    key_version = _optional_text(identity.get("key_version"), maximum=64)
    key_fingerprint = _optional_text(identity.get("key_fingerprint"), maximum=64)
    pv_uid = _optional_text(identity.get("pv_uid"), maximum=256)
    csi_driver = _optional_text(payload.get("csi_driver"), maximum=253)
    valid_for_interval = valid_for_interval and all(
        value is not None
        for value in (identity_scheme, key_version, key_fingerprint, pv_uid)
    )
    return StorageProjection(
        source_kind="volume",
        source_uid=item.source_uid,
        api_version=api_version,
        resource_version=resource_version,
        namespace=None,
        name=name,
        measurement_basis="volume-provisioned",
        resource_class="persistent-volume",
        cost_domain="physical-asset",
        resource="unmapped_block_volume",
        mapping_version=None,
        mapping_fingerprint=None,
        storage_bytes=storage_bytes,
        capacity_source=capacity_source,
        measurement_algorithm=algorithm,
        creation_timestamp=creation,
        deletion_requested=lifecycle.get("deletion_requested") is True,
        storage_class=storage_class,
        access_modes=access_modes,
        volume_mode=volume_mode,
        bound_volume_name=None,
        reclaim_policy=_normalized_reclaim_policy(payload.get("reclaim_policy")),
        backend_deletion_finalizer_observed=(
            lifecycle.get("has_deletion_protection_finalizer") is True
        ),
        claim_reference=claim_reference,
        identity_scheme=identity_scheme,
        identity_key_version=key_version,
        identity_key_fingerprint=key_fingerprint,
        pv_uid=pv_uid,
        csi_driver=csi_driver,
        owner_hint=None,
        static_attribution=None,
        classification_reason=(
            "bound-volume" if claim_reference is not None else "unbound-volume"
        ),
        attribution_ambiguous=claim_reference is None,
        valid_for_interval=valid_for_interval,
    )


def _normalized_reclaim_policy(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    lowered = value.lower()
    return lowered if lowered in {"delete", "retain", "recycle"} else "unknown"


def _normalized_volume_mode(value: str | None) -> str:
    if value is None:
        return "unknown"
    lowered = value.lower()
    return lowered if lowered in {"filesystem", "block"} else "unknown"


def _microseconds_between(later: datetime, earlier: datetime) -> int:
    delta = later - earlier
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _json_details(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


_VM_ROOTDISK_JOB_OWNER_SQL = """
/* infrastructure-metering:resolve-vm-rootdisk-job-owner */
SELECT 'job'::text AS owner_kind, job.id AS owner_id, job.user_id,
       job.project_id, job.context->'vm' AS vm_context
FROM jobs AS job
WHERE job.id = $1::uuid
FOR SHARE
"""

_VM_ROOTDISK_THREAD_OWNER_SQL = """
/* infrastructure-metering:resolve-vm-rootdisk-thread-owner */
SELECT 'thread'::text AS owner_kind, thread.id AS owner_id, thread.user_id,
       thread.project_id, thread.metadata->'vm' AS vm_context
FROM threads AS thread
WHERE thread.id = $1::uuid
FOR SHARE
"""


class StorageIntervalReconciler:
    """Snapshot/WATCH hooks for PVC demand and physical volume assets.

    ``interval_mutations_enabled`` is a one-way runtime fence for inventory
    authorities that are not eligible for activation or publication. Those
    authorities still exercise normalization, trusted attribution, shadow
    evidence, and the durable-volume registry, but can never open a billable
    resource interval even if another cluster already activated the same
    measurement basis.
    """

    def __init__(
        self,
        *,
        shadow_enabled: bool = True,
        interval_mutations_enabled: bool = True,
    ) -> None:
        self.shadow_enabled = shadow_enabled
        self.interval_mutations_enabled = interval_mutations_enabled

    @staticmethod
    async def _resolve_volume_mapping(
        conn: asyncpg.Connection,
        *,
        source_cluster: str,
        projection: StorageProjection,
    ) -> StorageProjection:
        if projection.source_kind != "volume" or not projection.valid_for_interval:
            return projection
        volume_mode = _normalized_volume_mode(projection.volume_mode)
        if volume_mode not in {"filesystem", "block"}:
            return projection
        resolution = await resolve_storage_resource_mapping(
            conn,
            StorageResourceMappingKey(
                source_cluster=source_cluster,
                storage_class_name=projection.storage_class,
                csi_driver=projection.csi_driver,
                volume_mode=volume_mode,
            ),
        )
        return replace(
            projection,
            resource=resolution.resource,
            mapping_version=resolution.mapping_version,
            mapping_fingerprint=resolution.rule_fingerprint,
        )

    @staticmethod
    async def _first_active_start(
        conn: asyncpg.Connection,
        *,
        context: SnapshotIntervalMutationContext | WatchIntervalMutationContext,
        item: InventoryItem,
        projection: StorageProjection,
    ) -> datetime:
        """Use the activation boundary only with continuous pre-boundary proof."""

        activation = await read_storage_activation(conn, projection.measurement_basis)
        global_boundary = activation.activated_at
        if (
            activation.state != "active"
            or global_boundary is None
            or activation.database_time is None
            or activation.database_time < global_boundary
        ):
            raise StorageActivationNotReady("storage basis is not active")
        boundary = await lock_storage_activation(
            conn,
            measurement_basis=projection.measurement_basis,
            inventory_scope_id=context.inventory_scope_id,
            observed_started_at=global_boundary,
        )
        if context.received_at < boundary:
            raise StorageActivationNotReady(
                "storage observation predates its source activation"
            )
        receipt_start = max(context.received_at, boundary)
        if (
            projection.creation_timestamp is not None
            and projection.creation_timestamp > boundary
        ):
            return receipt_start
        epoch = await conn.fetchrow(
            "SELECT required_for_rollup,required_from,reliable_from,"
            "continuous_since,continuity_health,"
            "EXISTS (SELECT 1 FROM resource_inventory_coverage_gaps gap "
            "WHERE gap.scope_epoch_id=epoch.id AND gap.gap_start < $2 "
            "AND COALESCE(gap.gap_end,$2) > $1) AS crossed_gap "
            "FROM resource_inventory_scope_epochs epoch WHERE id=$3 FOR SHARE",
            boundary,
            context.received_at,
            context.scope_epoch_id,
        )
        if (
            epoch is None
            or epoch["required_for_rollup"] is not True
            or epoch["required_from"] != boundary
            or epoch["reliable_from"] is None
            or epoch["reliable_from"] > boundary
            or epoch["continuous_since"] is None
            or epoch["continuous_since"] > boundary
            or epoch["continuity_health"] != "healthy"
            or epoch["crossed_gap"] is True
        ):
            return receipt_start
        evidence = await conn.fetchrow(
            "WITH evidence AS ("
            " SELECT snapshot.received_at AS observed_at,'present'::text AS state,"
            " item.revision_hash,0 AS priority "
            " FROM resource_inventory_snapshot_items item "
            " JOIN resource_inventory_snapshots snapshot "
            " ON snapshot.id=item.snapshot_id "
            " WHERE snapshot.scope_epoch_id=$1 AND snapshot.complete "
            " AND snapshot.manifest_state IN ('sealed','items-expired') "
            " AND snapshot.received_at <= $2 AND item.source_kind=$3 "
            " AND item.source_uid=$4 "
            " UNION ALL "
            " SELECT event.received_at,event.event_type,event.revision_hash,1 "
            " FROM resource_inventory_watch_events event "
            " WHERE event.scope_epoch_id=$1 AND event.received_at <= $2 "
            " AND event.source_kind=$3 AND event.source_uid=$4 "
            " AND event.event_type IN ('added','modified','deleted')"
            ") SELECT state,revision_hash FROM evidence "
            "ORDER BY observed_at DESC,priority DESC LIMIT 1",
            context.scope_epoch_id,
            boundary,
            item.source_kind,
            item.source_uid,
        )
        if (
            evidence is not None
            and evidence["state"] in {"present", "added", "modified"}
            and evidence["revision_hash"] == item.revision_hash
        ):
            return boundary
        return receipt_start

    @staticmethod
    async def _resolve_vm_rootdisk_owner(
        conn: asyncpg.Connection, projection: StorageProjection
    ) -> StorageAttribution:
        """Authenticate a root-PVC hint against durable application identity.

        The PVC remains present while its VM is suspended or deleted, so VM or
        VMI liveness is deliberately not part of this join.  The persisted VM
        name and namespace select the candidate, while the controller-attested
        immutable PVC UID binds that row to this exact claim incarnation.  A
        DataVolume is neither queried nor metered.
        """

        hint = projection.owner_hint
        if hint is None:
            return _unknown(projection.classification_reason)
        sql = (
            _VM_ROOTDISK_JOB_OWNER_SQL
            if hint.kind == "job"
            else _VM_ROOTDISK_THREAD_OWNER_SQL
        )
        row = await conn.fetchrow(sql, hint.id)
        if row is None:
            return _unknown("owner-row-missing")
        if row["owner_kind"] != hint.kind or _uuid_value(row["owner_id"]) != hint.id:
            return _unknown("owner-row-identity-mismatch")

        user_id = _uuid_value(row["user_id"])
        if user_id is None:
            return _unknown("owner-user-missing")
        project_value = row.get("project_id")
        project_id = _uuid_value(project_value)
        if project_value is not None and project_id is None:
            raise InventoryContractError(
                "VM rootdisk owner snapshot has invalid project identity"
            )

        vm_context = _json_details(row.get("vm_context"))
        if not vm_context:
            return _unknown("vm-context-missing")
        provision_generation = _canonical_uuid(vm_context.get("provision_generation"))
        identity_generation = _canonical_uuid(
            vm_context.get("identity_provision_generation")
        )
        if vm_context.get("identity_authenticated") is not True:
            return _unknown("vm-identity-unauthenticated")
        if provision_generation is None or identity_generation is None:
            return _unknown("vm-identity-generation-missing")
        if identity_generation != provision_generation:
            return _unknown("vm-identity-generation-mismatch")
        expected_vm_name = f"{_VM_ROOTDISK_PREFIX}{hint.id}"
        persisted_vm_name = vm_context.get("vm_name")
        if persisted_vm_name is None:
            persisted_vm_name = vm_context.get("name")
        if persisted_vm_name != expected_vm_name:
            return _unknown("vm-name-mismatch")
        if vm_context.get("namespace") != projection.namespace:
            return _unknown("vm-namespace-mismatch")
        persisted_pvc_uid_value = vm_context.get("rootdisk_pvc_uid")
        persisted_pvc_uid = _optional_text(persisted_pvc_uid_value, maximum=256)
        if persisted_pvc_uid is None:
            return _unknown(
                "rootdisk-pvc-uid-missing"
                if persisted_pvc_uid_value is None
                else "rootdisk-pvc-uid-invalid"
            )
        if persisted_pvc_uid != persisted_pvc_uid.strip() or any(
            character.isspace() for character in persisted_pvc_uid
        ):
            return _unknown("rootdisk-pvc-uid-invalid")
        if persisted_pvc_uid != projection.source_uid:
            return _unknown("rootdisk-pvc-uid-mismatch")

        # Current contexts derive identity from their owning row.  If a newer
        # producer also persists it explicitly, disagreement is a hard hint
        # conflict rather than a reason to trust the Kubernetes label.
        persisted_owner_kind = vm_context.get("owner_kind")
        if persisted_owner_kind is None:
            persisted_owner_kind = vm_context.get("entity_type")
        if persisted_owner_kind is not None and persisted_owner_kind != hint.kind:
            return _unknown("vm-owner-identity-mismatch")
        persisted_owner_id = vm_context.get("owner_id")
        if persisted_owner_id is None:
            persisted_owner_id = vm_context.get("entity_id")
        if persisted_owner_id is None:
            persisted_owner_id = vm_context.get("job_id")
        if persisted_owner_id is not None and str(persisted_owner_id) != str(hint.id):
            return _unknown("vm-owner-identity-mismatch")

        return StorageAttribution(
            scope="customer",
            owner_kind=hint.kind,
            owner_id=hint.id,
            user_id=user_id,
            project_id=project_id,
            source="app-db-vm-rootdisk-owner-identity",
            quality="exact",
            reason_code=f"{hint.kind}-vm-rootdisk-identity",
        )

    @staticmethod
    async def _resolve_claim_owner(
        conn: asyncpg.Connection, projection: StorageProjection
    ) -> StorageAttribution:
        if projection.static_attribution is not None:
            return projection.static_attribution
        if projection.resource == "vm_rootdisk_claim":
            return await StorageIntervalReconciler._resolve_vm_rootdisk_owner(
                conn, projection
            )
        hint = projection.owner_hint
        if hint is None:
            return _unknown(projection.classification_reason)
        table = "jobs" if hint.kind == "job" else "threads"
        row = await conn.fetchrow(
            f"SELECT id, user_id, project_id FROM {table} WHERE id=$1 FOR SHARE",
            hint.id,
        )
        if row is None or row.get("user_id") is None:
            return _unknown("owner-row-missing")
        user_id = row["user_id"]
        project_id = row.get("project_id")
        if not isinstance(user_id, UUID) or (
            project_id is not None and not isinstance(project_id, UUID)
        ):
            raise InventoryContractError("storage owner snapshot has invalid UUIDs")
        return StorageAttribution(
            scope="customer",
            owner_kind=hint.kind,
            owner_id=hint.id,
            user_id=user_id,
            project_id=project_id,
            source="app-db-owner-binding",
            quality="exact",
            reason_code=projection.classification_reason,
        )

    @staticmethod
    async def _resolve_volume_attribution(
        conn: asyncpg.Connection,
        *,
        source_cluster: str,
        projection: StorageProjection,
    ) -> StorageAttribution:
        if projection.claim_reference is None:
            return _unknown("unbound-volume")
        claim_uid, namespace, name = projection.claim_reference
        row = await conn.fetchrow(
            "SELECT attribution_scope, owner_kind, owner_id, user_id, project_id "
            "FROM resource_intervals WHERE source_cluster=$1 "
            "AND source_kind='pvc' AND source_uid=$2 AND namespace=$3 AND name=$4 "
            "AND measurement_basis='claim-requested' AND ended_at IS NULL FOR SHARE",
            source_cluster,
            claim_uid,
            namespace,
            name,
        )
        if row is None:
            return _unknown("claim-interval-missing")
        scope = str(row["attribution_scope"])
        if scope == "customer":
            try:
                owner_id = UUID(str(row["owner_id"]))
            except (TypeError, ValueError) as exc:
                raise InventoryContractError(
                    "validated claim interval has invalid owner identity"
                ) from exc
            user_id = row.get("user_id")
            project_id = row.get("project_id")
            if not isinstance(user_id, UUID) or (
                project_id is not None and not isinstance(project_id, UUID)
            ):
                raise InventoryContractError(
                    "validated claim interval has invalid owner snapshot"
                )
            kind = str(row["owner_kind"])
            if kind not in {"job", "thread"}:
                raise InventoryContractError(
                    "validated claim interval has invalid owner kind"
                )
            return StorageAttribution(
                scope="customer",
                owner_kind=kind,  # type: ignore[arg-type]
                owner_id=owner_id,
                user_id=user_id,
                project_id=project_id,
                source="validated-claim-interval",
                quality="derived",
                reason_code="bound-validated-claim",
            )
        if scope == "shared-platform":
            return _shared("bound-shared-claim")
        return _unknown("bound-claim-unknown")

    async def _resolve_attribution(
        self,
        conn: asyncpg.Connection,
        *,
        source_cluster: str,
        projection: StorageProjection,
    ) -> StorageAttribution:
        if projection.source_kind == "pvc":
            return await self._resolve_claim_owner(conn, projection)
        return await self._resolve_volume_attribution(
            conn,
            source_cluster=source_cluster,
            projection=projection,
        )

    @staticmethod
    async def _ensure_volume_asset(
        conn: asyncpg.Connection,
        *,
        context: SnapshotIntervalMutationContext
        | SnapshotObservationContext
        | WatchIntervalMutationContext,
        projection: StorageProjection,
    ) -> VolumeAssetRecord:
        if (
            projection.source_kind != "volume"
            or projection.identity_scheme is None
            or projection.identity_key_version is None
            or projection.identity_key_fingerprint is None
            or projection.pv_uid is None
            or projection.name is None
            or projection.storage_bytes is None
        ):
            raise InventoryContractError("valid PV projection lacks asset identity")
        await register_storage_identity_key(
            conn,
            key_version=projection.identity_key_version,
            key_fingerprint=projection.identity_key_fingerprint,
        )
        identity = derive_volume_asset_identity(
            source_cluster=context.source_cluster,
            normalized_source_uid=projection.source_uid,
            identity_scheme=projection.identity_scheme,
            identity_key_version=projection.identity_key_version,
            csi_driver=projection.csi_driver,
        )
        asset = await ensure_volume_asset(
            conn,
            identity=identity,
            observed_at=context.received_at,
        )
        if asset.lifecycle_state == "backend-unverified":
            await resolve_backend_gap_reobserved(
                conn,
                asset_id=asset.id,
                observed_at=context.received_at,
            )
        resource_version = projection.resource_version
        await observe_volume_incarnation(
            conn,
            asset_id=asset.id,
            inventory_scope_id=context.inventory_scope_id,
            source_cluster=context.source_cluster,
            pv_uid=projection.pv_uid,
            pv_name=projection.name,
            storage_class_name=projection.storage_class,
            reclaim_policy=projection.reclaim_policy,
            backend_deletion_finalizer_observed=(
                projection.backend_deletion_finalizer_observed
            ),
            volume_mode=_normalized_volume_mode(projection.volume_mode),
            capacity_bytes=projection.storage_bytes,
            bound_claim_uid=(
                projection.claim_reference[0]
                if projection.claim_reference is not None
                else None
            ),
            source_resource_version=(
                resource_version
                if resource_version is not None and len(resource_version) <= 255
                else None
            ),
            observed_at=context.received_at,
        )
        return asset

    @staticmethod
    def _attribution_matches(
        row: Mapping[str, Any], attribution: StorageAttribution
    ) -> bool:
        expected = (
            attribution.scope,
            attribution.owner_kind,
            None if attribution.owner_id is None else str(attribution.owner_id),
            attribution.user_id,
            attribution.project_id,
            attribution.source,
            attribution.quality,
        )
        actual = (
            row["attribution_scope"],
            row["owner_kind"],
            row["owner_id"],
            row["user_id"],
            row["project_id"],
            row["attribution_source"],
            row["attribution_quality"],
        )
        return actual == expected

    @staticmethod
    async def _confirm_existing(
        conn: asyncpg.Connection, interval_id: UUID, received_at: datetime
    ) -> UUID:
        value = await conn.fetchval(
            "UPDATE resource_intervals SET "
            "last_seen_at=GREATEST(last_seen_at,$2), "
            "last_confirmed_at=GREATEST(last_confirmed_at,$2), "
            "updated_at=statement_timestamp() "
            "WHERE id=$1 AND ended_at IS NULL RETURNING id",
            interval_id,
            received_at,
        )
        if value is None:
            raise InventoryConflictError("storage interval confirmation lost its lock")
        return value

    @staticmethod
    async def _close_existing(
        conn: asyncpg.Connection,
        interval_id: UUID | None,
        ended_at: datetime,
        *,
        time_source: str,
        reason: str,
        exact: bool = False,
    ) -> UUID | None:
        if interval_id is None:
            return None
        if not _SAFE_CODE.fullmatch(reason) or not _SAFE_CODE.fullmatch(time_source):
            raise InventoryContractError("storage closure evidence code is invalid")
        closed = await conn.fetchrow(
            "UPDATE resource_intervals SET ended_at=$2, end_time_source=$3, "
            "end_uncertainty_us=CASE WHEN $5 THEN 0 ELSE floor(extract(epoch FROM "
            "($2-last_confirmed_at))*1000000)::bigint END, end_reason=$4, "
            "updated_at=statement_timestamp() WHERE id=$1 AND ended_at IS NULL "
            "AND $2 >= last_confirmed_at RETURNING id, source_lifecycle_id",
            interval_id,
            ended_at,
            time_source,
            reason,
            exact,
        )
        if closed is None:
            raise InventoryConflictError("storage interval closure lost its lock")
        cleared = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET current_interval_id=NULL, "
            "updated_at=statement_timestamp() WHERE source_lifecycle_id=$1 "
            "AND current_interval_id=$2 RETURNING TRUE",
            closed["source_lifecycle_id"],
            closed["id"],
        )
        if not cleared:
            raise InventoryConflictError("storage lifecycle head was inconsistent")
        return closed["id"]

    @staticmethod
    async def _open_interval(
        conn: asyncpg.Connection,
        *,
        inventory_scope_id: UUID,
        source_cluster: str,
        received_at: datetime,
        started_at: datetime,
        item: InventoryItem,
        projection: StorageProjection,
        attribution: StorageAttribution,
        asset: VolumeAssetRecord | None,
    ) -> UUID:
        if (
            not projection.valid_for_interval
            or projection.api_version is None
            or projection.name is None
            or projection.storage_bytes is None
            or projection.capacity_source is None
            or projection.measurement_algorithm is None
            or item.revision_hash is None
        ):
            raise InventoryContractError("invalid storage projection cannot open")
        lifecycle_id = (
            asset.source_lifecycle_id
            if asset is not None
            else uuid5(
                _PVC_LIFECYCLE_NAMESPACE,
                f"{source_cluster}\x00pvc\x00{projection.source_uid}",
            )
        )
        await conn.execute(
            "INSERT INTO resource_lifecycle_heads "
            "(source_lifecycle_id,latest_revision_no) VALUES ($1,0) "
            "ON CONFLICT (source_lifecycle_id) DO NOTHING",
            lifecycle_id,
        )
        revision_no = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET "
            "latest_revision_no=latest_revision_no+1, "
            "updated_at=statement_timestamp() WHERE source_lifecycle_id=$1 "
            "AND current_interval_id IS NULL RETURNING latest_revision_no",
            lifecycle_id,
        )
        if revision_no is None:
            raise InventoryConflictError("storage lifecycle head is still open")
        interval_id = uuid4()
        uncertainty = 0
        if (
            projection.creation_timestamp is not None
            and projection.creation_timestamp <= started_at
        ):
            uncertainty = _microseconds_between(
                started_at, projection.creation_timestamp
            )
        details: dict[str, Any] = {
            "slice": "kubernetes-storage-v1",
            "classification_reason": projection.classification_reason,
            "storage_class": projection.storage_class,
            "access_modes": list(projection.access_modes),
            "volume_mode": projection.volume_mode,
            "deletion_requested": projection.deletion_requested,
            "publication_gate_required": True,
            "mapping_version": projection.mapping_version,
            "mapping_fingerprint": projection.mapping_fingerprint,
        }
        if projection.bound_volume_name is not None:
            details["bound_volume_name"] = projection.bound_volume_name
        if projection.claim_reference is not None:
            details["claim_reference"] = {
                "uid": projection.claim_reference[0],
                "namespace": projection.claim_reference[1],
                "name": projection.claim_reference[2],
            }
        if projection.source_kind == "volume":
            assert asset is not None
            details.update(
                {
                    "storage_asset_id": str(asset.id),
                    "asset_digest": asset.asset_digest,
                    "identity_scheme": asset.identity_scheme,
                    "identity_key_version": asset.identity_key_version,
                    "pv_uid": projection.pv_uid,
                    "reclaim_policy": projection.reclaim_policy,
                    "backend_deletion_finalizer_observed": (
                        projection.backend_deletion_finalizer_observed
                    ),
                    "csi_driver": projection.csi_driver,
                }
            )
        await conn.execute(
            "INSERT INTO resource_intervals ("
            "id,inventory_scope_id,source_cluster,source_kind,source_uid,"
            "source_api_version,source_resource_version,source_lifecycle_id,"
            "revision_no,source_revision,namespace,name,category,resource,"
            "measurement_basis,cost_domain,resource_class,attribution_scope,"
            "owner_kind,owner_id,user_id,project_id,attribution_source,"
            "attribution_quality,backing_resource_uid,lifecycle_confidence,"
            "storage_bytes,capacity_source,capacity_quality,measurement_algorithm,"
            "started_at,start_time_source,start_uncertainty_us,last_seen_at,"
            "last_confirmed_at,last_seen_snapshot_id,materialized_through,details) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'storage',$13,"
            "$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,'kubernetes-visible',"
            "$25,$26,'exact',$27,$28,$29,$30,$31,$31,NULL,$28,$32::jsonb)",
            interval_id,
            inventory_scope_id,
            source_cluster,
            projection.source_kind,
            projection.source_uid,
            projection.api_version,
            projection.resource_version,
            lifecycle_id,
            revision_no,
            item.revision_hash,
            projection.namespace,
            projection.name,
            projection.resource,
            projection.measurement_basis,
            projection.cost_domain,
            projection.resource_class,
            attribution.scope,
            attribution.owner_kind,
            None if attribution.owner_id is None else str(attribution.owner_id),
            attribution.user_id,
            attribution.project_id,
            attribution.source,
            attribution.quality,
            (
                projection.claim_reference[0]
                if projection.source_kind == "volume"
                and projection.claim_reference is not None
                else None
            ),
            projection.storage_bytes,
            projection.capacity_source,
            projection.measurement_algorithm,
            started_at,
            "app-db-received",
            uncertainty,
            received_at,
            json.dumps(details, sort_keys=True, separators=(",", ":")),
        )
        linked = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET current_interval_id=$2, "
            "updated_at=statement_timestamp() WHERE source_lifecycle_id=$1 "
            "AND current_interval_id IS NULL RETURNING TRUE",
            lifecycle_id,
            interval_id,
        )
        if not linked:
            raise InventoryConflictError("storage lifecycle head link failed")
        return interval_id

    async def _mutate(
        self,
        conn: asyncpg.Connection,
        *,
        context: SnapshotIntervalMutationContext | WatchIntervalMutationContext,
        item: InventoryItem,
    ) -> UUID | None:
        projection = project_storage_item(item)
        projection = await self._resolve_volume_mapping(
            conn,
            source_cluster=context.source_cluster,
            projection=projection,
        )
        asset = None
        if projection.source_kind == "volume" and projection.valid_for_interval:
            asset = await self._ensure_volume_asset(
                conn, context=context, projection=projection
            )
        attribution = await self._resolve_attribution(
            conn,
            source_cluster=context.source_cluster,
            projection=projection,
        )
        if not self.interval_mutations_enabled:
            # Remote VM-cluster storage is currently a dark inventory/shadow
            # source. Keep the asset/attribution proof above, but never consult
            # the global storage activation row or create resource_intervals.
            return None
        if not projection.valid_for_interval:
            await self._close_existing(
                conn,
                context.existing_interval_id,
                context.received_at,
                time_source="app-db-received",
                reason="invalid-storage-observation",
            )
            return None

        try:
            if context.existing_interval_id is None:
                started_at = await self._first_active_start(
                    conn,
                    context=context,
                    item=item,
                    projection=projection,
                )
            else:
                started_at = await lock_storage_activation(
                    conn,
                    measurement_basis=projection.measurement_basis,
                    inventory_scope_id=context.inventory_scope_id,
                    observed_started_at=context.received_at,
                )
        except StorageActivationNotReady:
            if context.existing_interval_id is not None:
                raise InventoryConflictError(
                    "storage activation regressed while an interval was open"
                )
            return None

        if context.existing_interval_id is not None:
            existing = await conn.fetchrow(
                "SELECT id,source_revision,resource,attribution_scope,owner_kind,"
                "owner_id,user_id,project_id,attribution_source,attribution_quality "
                "FROM resource_intervals WHERE id=$1 AND ended_at IS NULL FOR UPDATE",
                context.existing_interval_id,
            )
            if existing is None:
                raise InventoryConflictError("current storage interval disappeared")
            same_revision = existing["source_revision"] == item.revision_hash
            same_resource = existing["resource"] == projection.resource
            if (
                same_revision
                and same_resource
                and self._attribution_matches(existing, attribution)
            ):
                return await self._confirm_existing(
                    conn, context.existing_interval_id, context.received_at
                )
            await self._close_existing(
                conn,
                context.existing_interval_id,
                context.received_at,
                time_source="app-db-received",
                reason=("attribution-changed" if same_revision else "revision-changed"),
            )
            started_at = context.received_at

        return await self._open_interval(
            conn,
            inventory_scope_id=context.inventory_scope_id,
            source_cluster=context.source_cluster,
            received_at=context.received_at,
            started_at=started_at,
            item=item,
            projection=projection,
            attribution=attribution,
            asset=asset,
        )

    async def apply_snapshot(
        self,
        conn: asyncpg.Connection,
        context: SnapshotIntervalMutationContext,
        item: InventoryItem,
    ) -> UUID | None:
        return await self._mutate(conn, context=context, item=item)

    async def apply_watch(
        self,
        conn: asyncpg.Connection,
        context: WatchIntervalMutationContext,
        item: InventoryItem,
    ) -> UUID | None:
        return await self._mutate(conn, context=context, item=item)

    async def observe_snapshot(
        self,
        conn: asyncpg.Connection,
        context: SnapshotObservationContext,
        item: InventoryItem,
    ) -> None:
        """Write dedicated storage shadow evidence plus generic N/A evidence."""

        if not self.shadow_enabled:
            return
        projection = project_storage_item(item)
        projection = await self._resolve_volume_mapping(
            conn,
            source_cluster=context.source_cluster,
            projection=projection,
        )
        attribution = await self._resolve_attribution(
            conn,
            source_cluster=context.source_cluster,
            projection=projection,
        )
        asset = None
        if projection.source_kind == "volume" and projection.valid_for_interval:
            asset = await self._ensure_volume_asset(
                conn, context=context, projection=projection
            )
        if not item.valid_for_metering or not projection.valid_for_interval:
            disposition = "invalid"
            storage_bytes = None
        elif attribution.scope == "unknown":
            disposition = "identity-ambiguous"
            storage_bytes = projection.storage_bytes
        else:
            disposition = "eligible-unpriced"
            storage_bytes = projection.storage_bytes
        reason = attribution.reason_code
        if disposition == "invalid":
            reason = projection.classification_reason
        if not _SAFE_CODE.fullmatch(reason):
            reason = "invalid-item"
        await conn.execute(
            "INSERT INTO storage_shadow_observations ("
            "snapshot_id,inventory_scope_id,source_kind,source_uid,"
            "measurement_basis,asset_id,storage_bytes,resource,"
            "mapping_version,mapping_fingerprint,attribution_scope,owner_kind,"
            "owner_id,disposition,reason_code,observed_at"
            ") VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)",
            context.snapshot_id,
            context.inventory_scope_id,
            projection.source_kind,
            projection.source_uid,
            projection.measurement_basis,
            None if asset is None else asset.id,
            storage_bytes,
            projection.resource,
            projection.mapping_version,
            projection.mapping_fingerprint,
            attribution.scope,
            attribution.owner_kind,
            None if attribution.owner_id is None else str(attribution.owner_id),
            disposition,
            reason,
            context.received_at,
        )
        await conn.execute(
            "INSERT INTO resource_inventory_shadow_comparisons ("
            "snapshot_id,inventory_scope_id,source_uid,owner_kind,owner_id,"
            "owner_trusted,status,reason_code,explained,comparison_at) "
            "VALUES ($1,$2,$3,NULL,NULL,FALSE,'not-applicable',"
            "'dedicated-storage-shadow',TRUE,$4)",
            context.snapshot_id,
            context.inventory_scope_id,
            projection.source_uid,
            context.received_at,
        )

    @staticmethod
    async def _registry_incarnation(
        conn: asyncpg.Connection,
        *,
        source_cluster: str,
        source_uid: str,
    ) -> Mapping[str, Any] | None:
        return await conn.fetchrow(
            "SELECT incarnation.id AS incarnation_id, incarnation.asset_id,"
            "incarnation.pv_uid,incarnation.reclaim_policy,"
            "incarnation.backend_deletion_finalizer_observed,"
            "incarnation.last_observed_at,asset.identity_scheme,asset.asset_digest "
            "FROM storage_volume_incarnations AS incarnation "
            "JOIN storage_volume_assets AS asset ON asset.id=incarnation.asset_id "
            "WHERE incarnation.source_cluster=$1 AND incarnation.detached_at IS NULL "
            "AND ((asset.identity_scheme='csi-hmac-sha256-v1' "
            "AND asset.asset_digest=$2) OR (asset.identity_scheme='pv-uid-v1' "
            "AND incarnation.pv_uid=$2)) FOR UPDATE OF incarnation",
            source_cluster,
            source_uid,
        )

    @staticmethod
    async def _detach_incarnation(
        conn: asyncpg.Connection,
        incarnation_id: UUID,
        detached_at: datetime,
    ) -> None:
        value = await conn.fetchval(
            "UPDATE storage_volume_incarnations SET "
            "detached_at=GREATEST(last_observed_at,$2),detach_reason='pv-deleted',"
            "updated_at=statement_timestamp() WHERE id=$1 AND detached_at IS NULL "
            "RETURNING TRUE",
            incarnation_id,
            detached_at,
        )
        if not value:
            raise InventoryConflictError("PV incarnation detachment lost its lock")

    @staticmethod
    async def _backend_gap(
        conn: asyncpg.Connection,
        *,
        asset_id: UUID,
        scope_epoch_id: UUID,
        boundary: datetime,
        reason_code: str,
    ) -> None:
        await open_backend_unverified_gap(
            conn,
            asset_id=asset_id,
            scope_epoch_id=scope_epoch_id,
            gap_start=boundary,
            reason_code=reason_code,
        )

    @staticmethod
    def _backend_gap_reasons(
        reclaim_policy: str,
        deletion_finalizer_observed: bool,
    ) -> tuple[str, str]:
        if reclaim_policy == "retain":
            return "retain-pv-disappeared", "retain-pv-backend-unverified"
        if reclaim_policy == "delete" and deletion_finalizer_observed:
            # Finalizer presence is useful evidence, but it is insufficient
            # without an explicit, versioned provisioner guarantee. Preserve
            # the unknown tail until a provider/CSI assertion confirms it.
            return "delete-finalizer-unverified", "delete-pv-backend-unverified"
        if reclaim_policy == "delete":
            return "delete-pv-disappeared", "delete-pv-backend-unverified"
        return "pv-disappeared-unverified", "pv-backend-unverified"

    async def complete_snapshot(
        self,
        conn: asyncpg.Connection,
        context: SnapshotCompletionContext,
    ) -> None:
        """Detach PV incarnations absent from a complete physical inventory."""

        if context.namespace is not None:
            return
        last_id: UUID | None = None
        while True:
            rows = await conn.fetch(
                "SELECT incarnation.id AS incarnation_id,incarnation.asset_id,"
                "incarnation.pv_uid,incarnation.reclaim_policy,"
                "incarnation.backend_deletion_finalizer_observed,"
                "incarnation.last_observed_at,asset.identity_scheme,"
                "asset.asset_digest FROM storage_volume_incarnations incarnation "
                "JOIN storage_volume_assets asset ON asset.id=incarnation.asset_id "
                "WHERE incarnation.inventory_scope_id=$1 "
                "AND incarnation.detached_at IS NULL "
                "AND ($3::uuid IS NULL OR incarnation.id>$3) "
                "AND NOT EXISTS (SELECT 1 FROM resource_inventory_snapshot_items item "
                "WHERE item.snapshot_id=$2 AND item.source_kind='volume' "
                "AND item.source_uid=CASE WHEN asset.identity_scheme="
                "'csi-hmac-sha256-v1' THEN asset.asset_digest ELSE incarnation.pv_uid END) "
                "ORDER BY incarnation.id LIMIT $4 FOR UPDATE OF incarnation",
                context.inventory_scope_id,
                context.snapshot_id,
                last_id,
                _BATCH_SIZE,
            )
            if not rows:
                return
            for row in rows:
                last_id = row["incarnation_id"]
                last_observed = row["last_observed_at"]
                gap_reason, _ = self._backend_gap_reasons(
                    str(row["reclaim_policy"]),
                    bool(row["backend_deletion_finalizer_observed"]),
                )
                await self._detach_incarnation(
                    conn,
                    row["incarnation_id"],
                    last_observed,
                )
                await self._backend_gap(
                    conn,
                    asset_id=row["asset_id"],
                    scope_epoch_id=context.scope_epoch_id,
                    boundary=last_observed,
                    reason_code=gap_reason,
                )

    async def apply_absence(
        self,
        conn: asyncpg.Connection,
        context: SnapshotAbsenceMutationContext,
        interval: asyncpg.Record,
    ) -> bool:
        """Freeze physical accrual until backend destruction is proven."""

        if not self.interval_mutations_enabled:
            return False
        if interval["source_kind"] != "volume":
            return False
        details = _json_details(interval["details"])
        try:
            asset_id = UUID(str(details["storage_asset_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise InventoryContractError("volume interval lacks storage asset") from exc
        boundary = interval["last_confirmed_at"]
        gap_reason, closure_reason = self._backend_gap_reasons(
            str(details.get("reclaim_policy", "unknown")),
            details.get("backend_deletion_finalizer_observed") is True,
        )
        await self._backend_gap(
            conn,
            asset_id=asset_id,
            scope_epoch_id=context.scope_epoch_id,
            boundary=boundary,
            reason_code=gap_reason,
        )
        await self._close_existing(
            conn,
            interval["id"],
            boundary,
            time_source="backend-unverified",
            reason=closure_reason,
            exact=True,
        )
        return True

    async def apply_deletion(
        self,
        conn: asyncpg.Connection,
        context: WatchDeletionMutationContext,
        interval: asyncpg.Record | None,
    ) -> tuple[WatchMutationAction, UUID | None]:
        """Detach PV state and conservatively close a trusted DELETED event."""

        registry = None
        if context.source_kind == "volume":
            registry = await self._registry_incarnation(
                conn,
                source_cluster=context.source_cluster,
                source_uid=context.source_uid,
            )
        backend_unverified = registry is not None
        boundary = (
            interval["last_confirmed_at"]
            if interval is not None and backend_unverified
            else registry["last_observed_at"]
            if backend_unverified
            else context.received_at
        )
        closure_reason = "watch-deleted"
        if registry is not None:
            gap_reason, closure_reason = self._backend_gap_reasons(
                str(registry["reclaim_policy"]),
                bool(registry["backend_deletion_finalizer_observed"]),
            )
            await self._detach_incarnation(
                conn,
                registry["incarnation_id"],
                boundary,
            )
            await self._backend_gap(
                conn,
                asset_id=registry["asset_id"],
                scope_epoch_id=context.scope_epoch_id,
                boundary=boundary,
                reason_code=gap_reason,
            )
        if not self.interval_mutations_enabled:
            # Asset disappearance remains durable evidence, but this dark
            # authority may never close an interval supplied by another path.
            return WatchMutationAction.ALREADY_ABSENT, None
        if interval is None:
            return WatchMutationAction.ALREADY_ABSENT, None
        closed_id = await self._close_existing(
            conn,
            interval["id"],
            boundary,
            time_source=(
                "backend-unverified" if backend_unverified else "watch-deleted"
            ),
            reason=closure_reason,
            exact=backend_unverified,
        )
        return WatchMutationAction.CLOSE, closed_id


__all__ = [
    "StorageAttribution",
    "StorageIntervalReconciler",
    "StorageProjection",
    "project_storage_item",
]
