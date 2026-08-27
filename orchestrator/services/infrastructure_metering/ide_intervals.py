"""On-demand IDE Pod interval reconciliation for Slice 3.

The admitted Pod object is authoritative for lifecycle and scheduler-request
capacity. Kubernetes labels are only product/owner hints: customer attribution
requires a canonical full job UUID, the deterministic IDE Pod name, the exact
inventory namespace, and a matching current ``jobs.context.ide_session``
Kubernetes-container identity.

This adapter owns only the independently activated ``ide-session`` subtype of
``workspace_pod``. It never handles ordinary workspace Pods, writes usage
events, or creates publication plans.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

import asyncpg

from .agent_intervals import (
    PodProductClass,
    PodProductClassification,
    classify_product_pod,
)
from .compute_activation import (
    ComputeActivation,
    ComputeActivationError,
    lock_compute_scope_epoch_authority,
    read_compute_activation,
)
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


_ACTIVATION_KEY = "ide_workspace_pod"
_PRODUCT_CLASS = "ide-session"
_RESOURCE = "workspace_pod"
_IDE_LIFECYCLE_NAMESPACE = UUID("dd96d269-2081-50ef-b6f6-68e9319a59de")


@dataclass(frozen=True, slots=True)
class IdePodProjection:
    classification: PodProductClassification
    source_uid: str
    api_version: str
    resource_version: str | None
    namespace: str
    name: str
    owner_hint: UUID | None
    accrues: bool
    terminal: bool
    cpu_millicores: int
    memory_bytes: int
    capacity_quality: str
    measurement_algorithm: str
    identity_consistent: bool
    creation_timestamp: datetime | None
    start_time: datetime | None
    scheduled_transition_timestamp: datetime | None

    @property
    def applies(self) -> bool:
        return self.classification.product_class == PodProductClass.IDE_WORKSPACE


@dataclass(frozen=True, slots=True)
class IdePodAttribution:
    scope: Literal["customer", "unknown"]
    owner_kind: Literal["job"] | None
    owner_id: UUID | None
    user_id: UUID | None
    project_id: UUID | None
    source: str
    quality: Literal["exact", "ambiguous"]
    reason_code: str


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
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


def _unknown(reason: str) -> IdePodAttribution:
    return IdePodAttribution(
        scope="unknown",
        owner_kind=None,
        owner_id=None,
        user_id=None,
        project_id=None,
        source="kubernetes-ide-identity-unresolved",
        quality="ambiguous",
        reason_code=reason,
    )


def project_ide_pod(item: InventoryItem) -> IdePodProjection:
    """Project admitted IDE capacity while retaining labels only as hints."""

    classification = classify_product_pod(item)
    payload = _mapping(item.normalized_item)
    api_version = payload.get("api_version")
    resource_version = payload.get("resource_version")
    namespace = payload.get("namespace")
    name = payload.get("name")
    if not all(
        isinstance(value, str) and value for value in (api_version, namespace, name)
    ):
        raise InventoryContractError("normalized IDE Pod identity is incomplete")
    if resource_version is not None and not isinstance(resource_version, str):
        raise InventoryContractError("normalized IDE Pod resource version is invalid")

    labels = _mapping(payload.get("labels"))
    owner_hint = _canonical_uuid(labels.get("srw/job-id"))
    identity_consistent = (
        classification.identity_consistent
        and owner_hint is not None
        and not labels.get("srw/thread-id")
        and not labels.get("srw.io/thread-id")
        and not labels.get("srw/purpose")
        and not labels.get("srw/managed-by")
    )
    lifecycle = _mapping(payload.get("lifecycle"))
    scheduled_condition = _mapping(lifecycle.get("pod_scheduled_condition"))
    capacity = _mapping(payload.get("capacity"))
    cpu = _nonnegative_int(capacity.get("cpu_millicores"))
    memory = _nonnegative_int(capacity.get("memory_bytes"))
    quality = capacity.get("capacity_quality")
    algorithm = capacity.get("measurement_algorithm") or payload.get(
        "measurement_algorithm"
    )
    if (
        not item.valid_for_metering
        or cpu is None
        or memory is None
        or not isinstance(quality, str)
        or not quality
        or not isinstance(algorithm, str)
        or not algorithm
    ):
        raise InventoryContractError("valid IDE Pod lacks admitted capacity")
    terminal = lifecycle.get("terminal") is True
    return IdePodProjection(
        classification=classification,
        source_uid=item.source_uid,
        api_version=api_version,
        resource_version=resource_version,
        namespace=namespace,
        name=name,
        owner_hint=owner_hint,
        accrues=lifecycle.get("accrues") is True and not terminal,
        terminal=terminal,
        cpu_millicores=cpu,
        memory_bytes=memory,
        capacity_quality=quality,
        measurement_algorithm=algorithm,
        identity_consistent=identity_consistent,
        creation_timestamp=parse_lifecycle_timestamp(
            lifecycle.get("creation_timestamp")
        ),
        start_time=parse_lifecycle_timestamp(lifecycle.get("start_time")),
        scheduled_transition_timestamp=(
            parse_lifecycle_timestamp(scheduled_condition.get("last_transition_time"))
            if scheduled_condition.get("status") == "True"
            else None
        ),
    )


_IDE_OWNER_SQL = """
/* infrastructure-metering:resolve-ide-pod */
SELECT id, user_id, project_id,
       context->'ide_session'->>'container_name' AS ide_container_name,
       context->'ide_session'->>'restore_type' AS ide_restore_type
FROM jobs
WHERE id = $1
FOR SHARE
"""


async def resolve_ide_pod_attribution(
    conn: asyncpg.Connection,
    projection: IdePodProjection,
    *,
    expected_namespace: str | None,
) -> IdePodAttribution:
    """Accept customer ownership only for the current exact K8s IDE identity."""

    if not projection.applies:
        return _unknown("not-ide-pod")
    if not projection.identity_consistent or projection.owner_hint is None:
        return _unknown("ide-identity-conflict")
    if expected_namespace is None or projection.namespace != expected_namespace:
        return _unknown("ide-namespace-mismatch")
    expected_name = f"ide-{str(projection.owner_hint)[:12]}"
    if projection.name != expected_name:
        return _unknown("ide-pod-name-mismatch")

    row = await conn.fetchrow(_IDE_OWNER_SQL, projection.owner_hint)
    if row is None:
        return _unknown("ide-job-missing")
    owner_id = _uuid_value(row["id"])
    user_id = _uuid_value(row["user_id"])
    project_id = _uuid_value(row["project_id"])
    if owner_id != projection.owner_hint or user_id is None:
        return _unknown("ide-job-owner-invalid")
    if (
        row["ide_container_name"] != projection.name
        or row["ide_restore_type"] != "k8s_container"
    ):
        return _unknown("ide-context-mismatch")
    return IdePodAttribution(
        scope="customer",
        owner_kind="job",
        owner_id=owner_id,
        user_id=user_id,
        project_id=project_id,
        source="app-db-ide-context-binding",
        quality="exact",
        reason_code="ide-job-context-binding",
    )


async def _read_activation(
    conn: asyncpg.Connection,
) -> ComputeActivation | None:
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


def _active_boundary(activation: ComputeActivation) -> datetime | None:
    if (
        activation.state != "active"
        or activation.activated_at is None
        or activation.database_time is None
        or activation.database_time < activation.activated_at
    ):
        return None
    return activation.activated_at.astimezone(timezone.utc)


class IdePodIntervalReconciler:
    """Activated IDE Pod interval mutations and per-item shadow evidence."""

    def __init__(
        self,
        *,
        shadow_enabled: bool,
        activation: ComputeActivation | None = None,
    ) -> None:
        self.shadow_enabled = shadow_enabled
        self._supplied_activation = activation

    async def _activation(self, conn: asyncpg.Connection) -> ComputeActivation | None:
        if self._supplied_activation is not None:
            if self._supplied_activation.activation_key != _ACTIVATION_KEY:
                return None
            return self._supplied_activation
        return await _read_activation(conn)

    async def _interval_boundary(
        self,
        conn: asyncpg.Connection,
        context: SnapshotIntervalMutationContext | WatchIntervalMutationContext,
    ) -> datetime | None:
        if self._supplied_activation is not None:
            boundary = _active_boundary(self._supplied_activation)
            if (
                boundary is None
                or context.scope_epoch_id
                not in self._supplied_activation.authorized_scope_epoch_ids
            ):
                return None
            return boundary
        return await lock_compute_scope_epoch_authority(
            conn,
            activation_key=_ACTIVATION_KEY,
            inventory_scope_id=context.inventory_scope_id,
            inventory_scope_epoch_id=context.scope_epoch_id,
        )

    @staticmethod
    async def _initial_start(
        conn: asyncpg.Connection,
        *,
        context: SnapshotIntervalMutationContext | WatchIntervalMutationContext,
        item: InventoryItem,
        projection: IdePodProjection,
        activation_boundary: datetime,
    ) -> LifecycleStart:
        if isinstance(context, WatchIntervalMutationContext):
            return await watch_lifecycle_start(
                conn,
                context=context,
                item=item,
                source_kind="pod",
                authority_boundary=activation_boundary,
                creation_at=projection.creation_timestamp,
                scheduled_at=projection.scheduled_transition_timestamp,
                status_start_at=projection.start_time,
                scheduled_source="pod-scheduled-transition",
            )
        return receipt_lifecycle_start(
            received_at=context.received_at,
            authority_boundary=activation_boundary,
            creation_at=projection.creation_timestamp,
            scheduled_at=projection.scheduled_transition_timestamp,
            status_start_at=projection.start_time,
            scheduled_source="pod-scheduled-transition",
        )

    @staticmethod
    def _attribution_matches(
        row: Mapping[str, Any], attribution: IdePodAttribution
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
            "WHERE id=$1 AND resource='workspace_pod' "
            "AND details->>'product_class'='ide-session' "
            "AND ended_at IS NULL RETURNING id",
            interval_id,
            received_at,
        )
        if confirmed is None:
            raise InventoryConflictError("IDE Pod confirmation lost its lock")
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
            "WHERE id=$1 AND resource='workspace_pod' "
            "AND details->>'product_class'='ide-session' "
            "AND ended_at IS NULL AND $2 >= last_confirmed_at "
            "RETURNING id,source_lifecycle_id",
            interval_id,
            received_at,
            reason,
        )
        if closed is None:
            raise InventoryConflictError("IDE Pod interval closure lost its lock")
        cleared = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET current_interval_id=NULL,"
            "updated_at=statement_timestamp() "
            "WHERE source_lifecycle_id=$1 AND current_interval_id=$2 RETURNING TRUE",
            closed["source_lifecycle_id"],
            closed["id"],
        )
        if not cleared:
            raise InventoryConflictError("IDE Pod lifecycle head was inconsistent")

    @staticmethod
    async def _open_interval(
        conn: asyncpg.Connection,
        *,
        context: SnapshotIntervalMutationContext | WatchIntervalMutationContext,
        item: InventoryItem,
        projection: IdePodProjection,
        attribution: IdePodAttribution,
        activation_boundary: datetime,
        start: LifecycleStart,
    ) -> UUID:
        if context.received_at < activation_boundary:
            raise InventoryConflictError("IDE Pod observation precedes activation")
        if not (activation_boundary <= start.started_at <= context.received_at):
            raise InventoryConflictError("IDE Pod start exceeds its safe bounds")
        lifecycle_id = uuid5(
            _IDE_LIFECYCLE_NAMESPACE,
            f"{context.source_cluster}:pod:{projection.source_uid}",
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
            raise InventoryConflictError("IDE Pod lifecycle head is still open")
        if item.revision_hash is None:
            raise InventoryContractError("IDE Pod interval lacks source revision")

        interval_id = uuid4()
        details = json.dumps(
            {
                "attribution_reason": attribution.reason_code,
                "product_class": _PRODUCT_CLASS,
                "start_evidence_source": start.evidence_source,
                "publication_enabled": False,
                "slice": "kubernetes-ide-shadow-v1",
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
            "$1,$2,$3,'pod',$4,$5,$6,$7,$8,$9,$10,$11,'compute','workspace_pod',"
            "'scheduler-request','workload-allocation','kubernetes-pod',$12,"
            "$13,$14,$15,$16,$17,$18,'kubernetes-visible',$19,$20,"
            "'pod-effective-request',$21,$22,$23,$24,$25,$26,$26,NULL,$23,"
            "$27::jsonb,$28)",
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
            raise InventoryConflictError("IDE Pod lifecycle head link failed")
        return interval_id

    async def _mutate(
        self,
        conn: asyncpg.Connection,
        *,
        context: SnapshotIntervalMutationContext | WatchIntervalMutationContext,
        item: InventoryItem,
    ) -> UUID | None:
        projection = project_ide_pod(item)
        if not projection.applies:
            return None
        boundary = await self._interval_boundary(conn, context)
        if boundary is None:
            return None
        if context.received_at < boundary:
            raise InventoryConflictError("IDE Pod observation precedes activation")
        if not projection.accrues:
            await self._close_existing(
                conn,
                context.existing_interval_id,
                context.received_at,
                reason="terminal-or-unscheduled",
            )
            return None
        attribution = await resolve_ide_pod_attribution(
            conn,
            projection,
            expected_namespace=context.namespace,
        )
        start: LifecycleStart | None = None
        if context.existing_interval_id is not None:
            existing = await conn.fetchrow(
                "SELECT id,source_revision,attribution_scope,owner_kind,owner_id,"
                "user_id,project_id,attribution_source,attribution_quality,"
                "compute_scope_epoch_id "
                "FROM resource_intervals WHERE id=$1 "
                "AND resource='workspace_pod' "
                "AND details->>'product_class'='ide-session' "
                "AND ended_at IS NULL FOR UPDATE",
                context.existing_interval_id,
            )
            if existing is None:
                raise InventoryConflictError("current IDE Pod interval disappeared")
            if (
                _uuid_value(existing["compute_scope_epoch_id"])
                != context.scope_epoch_id
            ):
                raise InventoryConflictError(
                    "current IDE Pod interval belongs to another inventory epoch"
                )
            same_revision = existing["source_revision"] == item.revision_hash
            if same_revision and self._attribution_matches(existing, attribution):
                return await self._confirm_existing(
                    conn,
                    context.existing_interval_id,
                    context.received_at,
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
                activation_boundary=boundary,
            )
        return await self._open_interval(
            conn,
            context=context,
            item=item,
            projection=projection,
            attribution=attribution,
            activation_boundary=boundary,
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
        """Write exactly one IDE activation shadow row for every Pod item."""

        if not self.shadow_enabled:
            return
        activation = await self._activation(conn)
        if activation is None or not _permits_shadow(activation):
            return

        classification = classify_product_pod(item)
        projection: IdePodProjection | None = None
        if item.valid_for_metering:
            projection = project_ide_pod(item)

        if not item.valid_for_metering:
            attribution = _unknown("invalid-observation")
            disposition = "invalid"
            cpu = memory = None
            reason = (
                item.item_error.code
                if item.item_error is not None
                else "invalid-observation"
            )
        elif projection is None or not projection.applies:
            attribution = _unknown("not-ide-pod")
            disposition = "not-applicable"
            cpu = memory = None
            reason = classification.reason_code
        elif not projection.accrues:
            attribution = _unknown("terminal-or-unscheduled")
            disposition = "not-applicable"
            cpu = memory = None
            reason = "terminal-or-unscheduled"
        else:
            attribution = await resolve_ide_pod_attribution(
                conn,
                projection,
                expected_namespace=context.namespace,
            )
            reason = attribution.reason_code
            cpu = projection.cpu_millicores
            memory = projection.memory_bytes
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
                "'ide_workspace_pod',$1,$2,'pod',$3,'workspace_pod',"
                "'ide-session',$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                context.snapshot_id,
                context.inventory_scope_id,
                item.source_uid,
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
        except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError):
            # Mixed-version replicas remain dark instead of producing partial
            # shadow proof or falling back to the Slice 1 comparison table.
            return


__all__ = [
    "IdePodAttribution",
    "IdePodIntervalReconciler",
    "IdePodProjection",
    "project_ide_pod",
    "resolve_ide_pod_attribution",
]
