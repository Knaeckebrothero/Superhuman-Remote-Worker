"""Dark-launched VMI attribution, shadow evidence, and lifecycle intervals.

The admitted KubeVirt VMI is the compute authority.  Mutable Kubernetes owner
labels are only routing hints: customer attribution is accepted only when the
hint resolves to a job/thread row whose persisted VM identity agrees with the
VMI's owning VirtualMachine reference.  Identity conflicts remain metered as
``unknown`` and never fall back to a guessed customer.

This adapter has no usage-event or publication-plan path.  It writes immutable
shadow observations in ``shadow``/``active`` mode and mutates
``workspace_vm`` intervals only after the independent activation boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

import asyncpg

from .collectors.vmi_normalization import VMI_CAPACITY_ALGORITHM
from .compute_activation import (
    ComputeActivation,
    ComputeActivationError,
    lock_compute_scope_epoch_authority,
    read_compute_activation,
)
from .ingestion_types import validate_normalized_vmi_payload
from .inventory import (
    InventoryConflictError,
    InventoryContractError,
    InventoryItem,
    SnapshotIntervalMutationContext,
    SnapshotObservationContext,
    WatchIntervalMutationContext,
)
from .lifecycle_start import (
    LifecycleStart,
    parse_lifecycle_timestamp,
    receipt_lifecycle_start,
    watch_lifecycle_start,
)


_ACTIVATION_KEY = "workspace_vm"
_RESOURCE = "workspace_vm"
_PRODUCT_CLASS = "workspace-vm"
_VMI_LIFECYCLE_NAMESPACE = UUID("aa99f816-bfde-54ae-884f-622ab5460127")


@dataclass(frozen=True, slots=True)
class VMIProjection:
    """Metering-significant fields from one strict normalized VMI payload."""

    source_uid: str
    api_version: str
    resource_version: str
    namespace: str
    name: str
    lifecycle_state: Literal["unscheduled", "active", "terminal"]
    accrues: bool
    terminal: bool
    phase: str | None
    paused: bool
    migrating: bool
    cpu_millicores: int
    memory_bytes: int
    cpu_source: str
    memory_source: str
    capacity_quality: str
    measurement_algorithm: str
    owner_kind: Literal["job", "thread"] | None
    owner_id: UUID | None
    owner_hint_present: bool
    owner_hint_valid: bool
    vm_reference_uid: str | None
    vm_reference_name: str | None
    creation_timestamp: datetime | None
    scheduled_transition_timestamp: datetime | None
    valid_for_interval: bool


@dataclass(frozen=True, slots=True)
class VMIAttribution:
    """Forward-only attribution snapshot for one VMI interval revision."""

    scope: Literal["customer", "unknown"]
    owner_kind: Literal["job", "thread"] | None
    owner_id: UUID | None
    user_id: UUID | None
    project_id: UUID | None
    source: str
    quality: Literal["exact", "ambiguous"]
    reason_code: str


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {}
        return decoded if isinstance(decoded, Mapping) else {}
    return {}


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _uuid_value(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _canonical_uuid(value: Any) -> UUID | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    parsed = _uuid_value(value)
    return parsed if parsed is not None and str(parsed) == value else None


def _unknown(reason_code: str) -> VMIAttribution:
    return VMIAttribution(
        scope="unknown",
        owner_kind=None,
        owner_id=None,
        user_id=None,
        project_id=None,
        source="app-db-vm-identity-conflict",
        quality="ambiguous",
        reason_code=reason_code,
    )


def project_vmi(item: InventoryItem) -> VMIProjection:
    """Validate and project the raw-free admitted VMI inventory contract."""

    if item.source_kind != "vmi":
        raise InventoryContractError("VMI adapter received another source kind")
    payload = dict(item.normalized_item)
    try:
        validate_normalized_vmi_payload(payload)
    except ValueError as exc:
        raise InventoryContractError("normalized VMI payload is invalid") from exc

    uid = payload["uid"]
    revision_hash = payload["revision_hash"]
    if uid != item.source_uid:
        raise InventoryContractError("normalized VMI UID disagrees with inventory")
    if revision_hash != item.revision_hash:
        raise InventoryContractError("normalized VMI revision disagrees with inventory")

    lifecycle = _mapping(payload["lifecycle"])
    capacity = _mapping(payload.get("capacity"))
    cpu = _positive_int(capacity.get("cpu_millicores"))
    memory = _positive_int(capacity.get("memory_bytes"))
    quality = capacity.get("capacity_quality")
    algorithm = capacity.get("measurement_algorithm")
    cpu_source = capacity.get("cpu_source")
    memory_source = capacity.get("memory_source")
    capacity_valid = (
        cpu is not None
        and memory is not None
        and cpu_source in {"vmi-status-current-topology", "vmi-admitted-topology"}
        and memory_source in {"vmi-status-guest-current", "vmi-admitted-guest-memory"}
        and quality == "exact"
        and algorithm == VMI_CAPACITY_ALGORITHM
    )
    if item.valid_for_metering and not capacity_valid:
        raise InventoryContractError("meterable VMI lacks admitted guest capacity")

    owner_hint = _mapping(payload.get("owner_hint"))
    owner_hint_present = payload.get("owner_hint") is not None
    owner_kind_value = owner_hint.get("kind")
    owner_id = _canonical_uuid(owner_hint.get("owner_id"))
    owner_hint_valid = owner_kind_value in {"job", "thread"} and owner_id is not None
    owner_kind: Literal["job", "thread"] | None = None
    if owner_hint_valid:
        owner_kind = owner_kind_value  # type: ignore[assignment]

    vm_reference = _mapping(payload.get("vm_reference"))
    vm_uid = vm_reference.get("uid")
    vm_name = vm_reference.get("name")
    if not isinstance(vm_uid, str) or not vm_uid:
        vm_uid = None
    if not isinstance(vm_name, str) or not vm_name:
        vm_name = None

    state = lifecycle["state"]
    terminal = state == "terminal"
    accrues = (
        state == "active"
        and lifecycle.get("scheduled") is True
        and lifecycle.get("accrues") is True
        and not terminal
    )
    return VMIProjection(
        source_uid=item.source_uid,
        api_version=str(payload["api_version"]),
        resource_version=str(payload["resource_version"]),
        namespace=str(payload["namespace"]),
        name=str(payload["name"]),
        lifecycle_state=state,
        accrues=accrues,
        terminal=terminal,
        phase=(
            str(lifecycle["phase"]) if isinstance(lifecycle.get("phase"), str) else None
        ),
        paused=lifecycle.get("paused") is True,
        migrating=lifecycle.get("migrating") is True,
        cpu_millicores=cpu or 0,
        memory_bytes=memory or 0,
        cpu_source=str(cpu_source or "invalid"),
        memory_source=str(memory_source or "invalid"),
        capacity_quality=str(quality or "invalid"),
        measurement_algorithm=str(algorithm or VMI_CAPACITY_ALGORITHM),
        owner_kind=owner_kind,
        owner_id=owner_id,
        owner_hint_present=owner_hint_present,
        owner_hint_valid=owner_hint_valid,
        vm_reference_uid=vm_uid,
        vm_reference_name=vm_name,
        creation_timestamp=parse_lifecycle_timestamp(
            lifecycle.get("creation_timestamp")
        ),
        scheduled_transition_timestamp=parse_lifecycle_timestamp(
            lifecycle.get("scheduled_transition_timestamp")
        ),
        valid_for_interval=item.valid_for_metering and capacity_valid,
    )


_VM_OWNER_SQL = """
/* infrastructure-metering:resolve-vmi-owner */
SELECT 'job'::text AS owner_kind, job.id AS owner_id, job.user_id,
       job.project_id, job.context->'vm' AS vm_context
FROM jobs AS job
WHERE $1::text = 'job' AND job.id = $2::uuid
UNION ALL
SELECT 'thread'::text AS owner_kind, thread.id AS owner_id, thread.user_id,
       thread.project_id, thread.metadata->'vm' AS vm_context
FROM threads AS thread
WHERE $1::text = 'thread' AND thread.id = $2::uuid
LIMIT 2
"""


async def resolve_vmi_attribution(
    conn: asyncpg.Connection,
    projection: VMIProjection,
) -> VMIAttribution:
    """Resolve a VMI hint through authoritative owner and persisted VM state."""

    if not projection.owner_hint_present:
        return _unknown("owner-hint-missing")
    if (
        not projection.owner_hint_valid
        or projection.owner_kind is None
        or projection.owner_id is None
    ):
        return _unknown("owner-hint-invalid")
    if projection.vm_reference_uid is None or projection.vm_reference_name is None:
        return _unknown("vm-reference-missing")
    if projection.vm_reference_name != projection.name:
        return _unknown("vmi-vm-name-mismatch")

    rows = await conn.fetch(_VM_OWNER_SQL, projection.owner_kind, projection.owner_id)
    if len(rows) == 0:
        return _unknown("owner-row-missing")
    if len(rows) != 1:
        return _unknown("owner-row-ambiguous")
    row = rows[0]
    row_owner = _uuid_value(row["owner_id"])
    if row["owner_kind"] != projection.owner_kind or row_owner != projection.owner_id:
        return _unknown("owner-row-identity-mismatch")

    user_id = _uuid_value(row["user_id"])
    if user_id is None:
        return _unknown("owner-user-missing")
    project_id = _uuid_value(row["project_id"])

    vm_context = _json_mapping(row["vm_context"])
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
    persisted_name = vm_context.get("vm_name") or vm_context.get("name")
    if persisted_name != projection.vm_reference_name:
        return _unknown("vm-name-mismatch")
    if vm_context.get("namespace") != projection.namespace:
        return _unknown("vm-namespace-mismatch")
    persisted_vm_uid = vm_context.get("vm_uid")
    if persisted_vm_uid is None:
        # Pre-Slice-3 controller contexts did not persist the admitted VM UID.
        # Keep those visible as an explicit legacy-unknown population; accepting
        # name + namespace alone as exact would make a reused VM name spoofable.
        return _unknown("vm-uid-missing-legacy")
    if persisted_vm_uid != projection.vm_reference_uid:
        return _unknown("vm-uid-mismatch")

    return VMIAttribution(
        scope="customer",
        owner_kind=projection.owner_kind,
        owner_id=projection.owner_id,
        user_id=user_id,
        project_id=project_id,
        source="app-db-vm-owner-identity",
        quality="exact",
        reason_code=f"{projection.owner_kind}-vm-identity",
    )


async def read_vm_compute_activation(
    conn: asyncpg.Connection,
) -> ComputeActivation | None:
    """Read the VM activation row, failing closed across mixed migrations."""

    try:
        activation = await read_compute_activation(conn, _ACTIVATION_KEY)
    except (
        ComputeActivationError,
        asyncpg.UndefinedColumnError,
        asyncpg.UndefinedTableError,
    ):
        return None
    if activation.activation_key != _ACTIVATION_KEY:
        return None
    if activation.state not in {"disabled", "shadow", "active"}:
        return None
    if activation.state == "active":
        if activation.activated_at is None or activation.database_time is None:
            return None
    elif activation.activated_at is not None:
        return None
    return activation


def _permits_shadow(activation: ComputeActivation) -> bool:
    return activation.state in {"shadow", "active"}


def _permits_intervals(activation: ComputeActivation) -> bool:
    return (
        activation.state == "active"
        and activation.activated_at is not None
        and activation.database_time is not None
        and activation.database_time >= activation.activated_at
    )


class VMIIntervalReconciler:
    """Activated VMI interval mutations plus immutable shadow evidence."""

    def __init__(
        self,
        *,
        shadow_enabled: bool,
        activation: ComputeActivation | None = None,
        max_lifecycle_clock_skew: timedelta = timedelta(minutes=5),
    ) -> None:
        if max_lifecycle_clock_skew < timedelta(0):
            raise ValueError("VMI lifecycle clock skew must be nonnegative")
        self.shadow_enabled = shadow_enabled
        self._supplied_activation = activation
        self.max_lifecycle_clock_skew = max_lifecycle_clock_skew

    async def _activation(self, conn: asyncpg.Connection) -> ComputeActivation | None:
        if self._supplied_activation is not None:
            if self._supplied_activation.activation_key != _ACTIVATION_KEY:
                return None
            return self._supplied_activation
        return await read_vm_compute_activation(conn)

    async def _interval_boundary(
        self,
        conn: asyncpg.Connection,
        context: SnapshotIntervalMutationContext | WatchIntervalMutationContext,
    ) -> datetime | None:
        if self._supplied_activation is not None:
            if (
                not _permits_intervals(self._supplied_activation)
                or context.scope_epoch_id
                not in self._supplied_activation.authorized_scope_epoch_ids
            ):
                return None
            return self._supplied_activation.activated_at
        return await lock_compute_scope_epoch_authority(
            conn,
            activation_key=_ACTIVATION_KEY,
            inventory_scope_id=context.inventory_scope_id,
            inventory_scope_epoch_id=context.scope_epoch_id,
        )

    async def _initial_start(
        self,
        conn: asyncpg.Connection,
        *,
        context: SnapshotIntervalMutationContext | WatchIntervalMutationContext,
        item: InventoryItem,
        projection: VMIProjection,
        activation_boundary: datetime,
    ) -> LifecycleStart:
        if isinstance(context, WatchIntervalMutationContext):
            return await watch_lifecycle_start(
                conn,
                context=context,
                item=item,
                source_kind="vmi",
                authority_boundary=activation_boundary,
                creation_at=projection.creation_timestamp,
                scheduled_at=projection.scheduled_transition_timestamp,
                scheduled_source="vmi-scheduled-transition",
                max_scheduled_clock_skew=self.max_lifecycle_clock_skew,
            )
        return receipt_lifecycle_start(
            received_at=context.received_at,
            authority_boundary=activation_boundary,
            creation_at=projection.creation_timestamp,
            scheduled_at=projection.scheduled_transition_timestamp,
            scheduled_source="vmi-scheduled-transition",
            max_scheduled_clock_skew=self.max_lifecycle_clock_skew,
        )

    @staticmethod
    def _attribution_matches(
        row: Mapping[str, Any], attribution: VMIAttribution
    ) -> bool:
        return (
            row["attribution_scope"],
            row["owner_kind"],
            row["owner_id"],
            row["user_id"],
            row["project_id"],
            row["attribution_source"],
            row["attribution_quality"],
        ) == (
            attribution.scope,
            attribution.owner_kind,
            None if attribution.owner_id is None else str(attribution.owner_id),
            attribution.user_id,
            attribution.project_id,
            attribution.source,
            attribution.quality,
        )

    @staticmethod
    async def _confirm_existing(
        conn: asyncpg.Connection,
        interval_id: UUID,
        received_at: datetime,
    ) -> UUID:
        confirmed = await conn.fetchval(
            "UPDATE resource_intervals SET "
            "last_seen_at=GREATEST(last_seen_at,$2),"
            "last_confirmed_at=GREATEST(last_confirmed_at,$2),"
            "updated_at=statement_timestamp() "
            "WHERE id=$1 AND source_kind='vmi' AND resource='workspace_vm' "
            "AND ended_at IS NULL RETURNING id",
            interval_id,
            received_at,
        )
        if confirmed is None:
            raise InventoryConflictError("VMI confirmation lost its interval lock")
        return confirmed

    @staticmethod
    async def _close_existing(
        conn: asyncpg.Connection,
        interval_id: UUID | None,
        received_at: datetime,
        *,
        reason: str,
    ) -> None:
        if interval_id is None:
            return
        closed = await conn.fetchrow(
            "UPDATE resource_intervals SET ended_at=$2,"
            "end_time_source='app-db-received',"
            "end_uncertainty_us=floor(extract(epoch FROM "
            "($2-last_confirmed_at))*1000000)::bigint,end_reason=$3,"
            "updated_at=statement_timestamp() "
            "WHERE id=$1 AND source_kind='vmi' AND resource='workspace_vm' "
            "AND ended_at IS NULL AND $2 >= last_confirmed_at "
            "RETURNING id,source_lifecycle_id",
            interval_id,
            received_at,
            reason,
        )
        if closed is None:
            raise InventoryConflictError("VMI closure lost its interval lock")
        cleared = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET current_interval_id=NULL,"
            "updated_at=statement_timestamp() "
            "WHERE source_lifecycle_id=$1 AND current_interval_id=$2 "
            "RETURNING TRUE",
            closed["source_lifecycle_id"],
            closed["id"],
        )
        if not cleared:
            raise InventoryConflictError("VMI lifecycle head was inconsistent")

    @staticmethod
    async def _open_interval(
        conn: asyncpg.Connection,
        *,
        context: SnapshotIntervalMutationContext | WatchIntervalMutationContext,
        item: InventoryItem,
        projection: VMIProjection,
        attribution: VMIAttribution,
        activated_at: datetime,
        start: LifecycleStart,
    ) -> UUID:
        if context.received_at < activated_at:
            raise InventoryConflictError("VMI observation precedes activation")
        if not (activated_at <= start.started_at <= context.received_at):
            raise InventoryConflictError("VMI start exceeds its safe bounds")
        lifecycle_id = uuid5(
            _VMI_LIFECYCLE_NAMESPACE,
            f"{context.source_cluster}:vmi:{projection.source_uid}",
        )
        await conn.execute(
            "INSERT INTO resource_lifecycle_heads "
            "(source_lifecycle_id,latest_revision_no) VALUES ($1,0) "
            "ON CONFLICT (source_lifecycle_id) DO NOTHING",
            lifecycle_id,
        )
        revision_no = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET "
            "latest_revision_no=latest_revision_no+1,"
            "updated_at=statement_timestamp() "
            "WHERE source_lifecycle_id=$1 AND current_interval_id IS NULL "
            "RETURNING latest_revision_no",
            lifecycle_id,
        )
        if revision_no is None:
            raise InventoryConflictError("VMI lifecycle head is already open")
        if not projection.valid_for_interval or item.revision_hash is None:
            raise InventoryContractError("VMI interval lacks admitted capacity")

        interval_id = uuid4()
        details = json.dumps(
            {
                "attribution_reason": attribution.reason_code,
                "migrating": projection.migrating,
                "paused": projection.paused,
                "phase": projection.phase,
                "start_evidence_source": start.evidence_source,
                "publication_enabled": False,
                "slice": "kubevirt-vmi-shadow-v1",
                "cpu_source": projection.cpu_source,
                "memory_source": projection.memory_source,
                "vm_reference_uid": projection.vm_reference_uid,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        await conn.execute(
            "INSERT INTO resource_intervals ("
            "id,inventory_scope_id,source_cluster,source_kind,source_uid,"
            "source_api_version,source_resource_version,source_lifecycle_id,"
            "revision_no,source_revision,namespace,name,category,resource,"
            "measurement_basis,cost_domain,resource_class,attribution_scope,"
            "owner_kind,owner_id,user_id,project_id,attribution_source,"
            "attribution_quality,lifecycle_confidence,cpu_millicores,"
            "memory_bytes,capacity_source,capacity_quality,measurement_algorithm,"
            "started_at,start_time_source,start_uncertainty_us,last_seen_at,"
            "last_confirmed_at,last_seen_snapshot_id,materialized_through,details,"
            "compute_scope_epoch_id"
            ") VALUES ("
            "$1,$2,$3,'vmi',$4,$5,$6,$7,$8,$9,$10,$11,'compute','workspace_vm',"
            "'guest-provisioned','workload-allocation','virtual-machine',$12,"
            "$13,$14,$15,$16,$17,$18,'kubernetes-visible',$19,$20,"
            "'vmi-current-or-admitted-guest',$21,$22,$23,$24,$25,$26,$26,"
            "NULL,$23,$27::jsonb,$28)",
            interval_id,
            context.inventory_scope_id,
            context.source_cluster,
            projection.source_uid,
            projection.api_version,
            projection.resource_version,
            lifecycle_id,
            revision_no,
            item.revision_hash,
            projection.namespace,
            projection.name,
            attribution.scope,
            attribution.owner_kind,
            None if attribution.owner_id is None else str(attribution.owner_id),
            attribution.user_id,
            attribution.project_id,
            attribution.source,
            attribution.quality,
            projection.cpu_millicores,
            projection.memory_bytes,
            projection.capacity_quality,
            projection.measurement_algorithm,
            start.started_at,
            start.time_source,
            start.uncertainty_us,
            context.received_at,
            details,
            context.scope_epoch_id,
        )
        linked = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET current_interval_id=$2,"
            "updated_at=statement_timestamp() "
            "WHERE source_lifecycle_id=$1 AND current_interval_id IS NULL "
            "RETURNING TRUE",
            lifecycle_id,
            interval_id,
        )
        if not linked:
            raise InventoryConflictError("VMI lifecycle head link failed")
        return interval_id

    async def _mutate(
        self,
        conn: asyncpg.Connection,
        *,
        context: SnapshotIntervalMutationContext | WatchIntervalMutationContext,
        item: InventoryItem,
    ) -> UUID | None:
        projection = project_vmi(item)
        activation_boundary = await self._interval_boundary(conn, context)
        if activation_boundary is None:
            return None
        if context.received_at < activation_boundary:
            raise InventoryConflictError("VMI observation precedes activation")
        if not projection.valid_for_interval:
            return None
        if not projection.accrues:
            await self._close_existing(
                conn,
                context.existing_interval_id,
                context.received_at,
                reason=("terminal" if projection.terminal else "no-longer-active"),
            )
            return None

        attribution = await resolve_vmi_attribution(conn, projection)
        start: LifecycleStart | None = None
        if context.existing_interval_id is not None:
            existing = await conn.fetchrow(
                "SELECT id,source_revision,attribution_scope,owner_kind,owner_id,"
                "user_id,project_id,attribution_source,attribution_quality,"
                "compute_scope_epoch_id "
                "FROM resource_intervals WHERE id=$1 AND source_kind='vmi' "
                "AND resource='workspace_vm' AND ended_at IS NULL FOR UPDATE",
                context.existing_interval_id,
            )
            if existing is None:
                raise InventoryConflictError("current VMI interval disappeared")
            if (
                _uuid_value(existing["compute_scope_epoch_id"])
                != context.scope_epoch_id
            ):
                raise InventoryConflictError(
                    "current VMI interval belongs to another inventory epoch"
                )
            same_revision = existing["source_revision"] == item.revision_hash
            if same_revision and self._attribution_matches(existing, attribution):
                return await self._confirm_existing(
                    conn, context.existing_interval_id, context.received_at
                )
            await self._close_existing(
                conn,
                context.existing_interval_id,
                context.received_at,
                reason=("attribution-changed" if same_revision else "revision-changed"),
            )
            start = LifecycleStart(
                started_at=context.received_at,
                time_source="app-db-received",
                uncertainty_us=0,
                evidence_source="observed-revision-boundary",
            )
        if start is None:
            start = await self._initial_start(
                conn,
                context=context,
                item=item,
                projection=projection,
                activation_boundary=activation_boundary,
            )
        return await self._open_interval(
            conn,
            context=context,
            item=item,
            projection=projection,
            attribution=attribution,
            activated_at=activation_boundary,
            start=start,
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
        """Write exactly one non-publishable VM row per staged VMI item."""

        if not self.shadow_enabled:
            return
        activation = await self._activation(conn)
        if activation is None or not _permits_shadow(activation):
            return

        if not item.valid_for_metering:
            attribution = _unknown("invalid-observation")
            disposition = "invalid"
            cpu = memory = None
            reason = (
                item.item_error.code
                if item.item_error is not None
                else "invalid-observation"
            )
        else:
            projection = project_vmi(item)
            if not projection.valid_for_interval:
                attribution = _unknown("invalid-observation")
                disposition = "invalid"
                cpu = memory = None
                reason = "invalid-observation"
            elif not projection.accrues:
                attribution = _unknown("terminal-or-unscheduled")
                disposition = "not-applicable"
                cpu = memory = None
                reason = "terminal-or-unscheduled"
            else:
                attribution = await resolve_vmi_attribution(conn, projection)
                cpu = projection.cpu_millicores
                memory = projection.memory_bytes
                reason = attribution.reason_code
                disposition = (
                    "eligible-unpriced"
                    if attribution.scope == "customer"
                    else "identity-ambiguous"
                )

        try:
            await conn.execute(
                "INSERT INTO compute_shadow_observations ("
                "activation_key,snapshot_id,inventory_scope_id,source_kind,"
                "source_uid,resource,product_class,cpu_millicores,memory_bytes,"
                "attribution_scope,owner_kind,owner_id,user_id,project_id,"
                "disposition,reason_code,observed_at) VALUES ("
                "'workspace_vm',$1,$2,'vmi',$3,'workspace_vm',$4,$5,$6,$7,"
                "$8,$9,$10,$11,$12,$13,$14)",
                context.snapshot_id,
                context.inventory_scope_id,
                item.source_uid,
                _PRODUCT_CLASS,
                cpu,
                memory,
                attribution.scope,
                attribution.owner_kind,
                attribution.owner_id,
                attribution.user_id,
                attribution.project_id,
                disposition,
                reason,
                context.received_at,
            )
            if item.valid_for_metering:
                # VMIs have no legacy interval ledger to compare against.  A
                # stable not-applicable row nevertheless resolves this valid
                # snapshot item for the generic item-for-item health check.
                # Keep the row projection-only so a replay cannot change when
                # application attribution state changes after the LIST.
                observed_started_at = (
                    projection.scheduled_transition_timestamp
                    or projection.creation_timestamp
                )
                observed_start_source = (
                    "vmi-scheduled-transition"
                    if projection.scheduled_transition_timestamp is not None
                    else "object-creation-timestamp"
                    if projection.creation_timestamp is not None
                    else None
                )
                await conn.execute(
                    "INSERT INTO resource_inventory_shadow_comparisons ("
                    "snapshot_id,inventory_scope_id,source_uid,owner_kind,"
                    "owner_id,owner_trusted,observed_cpu_millicores,"
                    "observed_memory_bytes,observed_started_at,"
                    "observed_start_time_source,observed_start_uncertainty_us,"
                    "status,reason_code,explained,comparison_at) VALUES ("
                    "$1,$2,$3,NULL,NULL,FALSE,$4,$5,$6,$7,$8,"
                    "'not-applicable','vmi-no-legacy-interval',TRUE,$9) "
                    "ON CONFLICT (snapshot_id,source_uid) DO NOTHING",
                    context.snapshot_id,
                    context.inventory_scope_id,
                    item.source_uid,
                    projection.cpu_millicores,
                    projection.memory_bytes,
                    observed_started_at,
                    observed_start_source,
                    0 if observed_started_at is not None else None,
                    context.received_at,
                )
                comparison_matches = await conn.fetchval(
                    "SELECT inventory_scope_id=$2 AND owner_kind IS NULL "
                    "AND owner_id IS NULL AND owner_trusted=FALSE "
                    "AND legacy_interval_id IS NULL "
                    "AND legacy_cpu_millicores IS NULL "
                    "AND legacy_memory_bytes IS NULL "
                    "AND legacy_started_at IS NULL "
                    "AND observed_cpu_millicores IS NOT DISTINCT FROM $4 "
                    "AND observed_memory_bytes IS NOT DISTINCT FROM $5 "
                    "AND observed_started_at IS NOT DISTINCT FROM $6 "
                    "AND observed_start_time_source IS NOT DISTINCT FROM $7 "
                    "AND observed_start_uncertainty_us IS NOT DISTINCT FROM $8 "
                    "AND start_delta_us IS NULL AND status='not-applicable' "
                    "AND reason_code='vmi-no-legacy-interval' AND explained=TRUE "
                    "AND comparison_at=$9 "
                    "FROM resource_inventory_shadow_comparisons "
                    "WHERE snapshot_id=$1 AND source_uid=$3",
                    context.snapshot_id,
                    context.inventory_scope_id,
                    item.source_uid,
                    projection.cpu_millicores,
                    projection.memory_bytes,
                    observed_started_at,
                    observed_start_source,
                    0 if observed_started_at is not None else None,
                    context.received_at,
                )
                if comparison_matches is not True:
                    raise InventoryConflictError(
                        "VMI shadow comparison replayed with different content"
                    )
        except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError):
            # Mixed-version rollout: stay dark and never fall back to legacy
            # workspace billing or an interval/publication side effect.
            return


__all__ = [
    "ComputeActivation",
    "VMIAttribution",
    "VMIIntervalReconciler",
    "VMIProjection",
    "project_vmi",
    "read_vm_compute_activation",
    "resolve_vmi_attribution",
]
