"""Shadow-only workspace Pod interval reconciliation for Slice 1.

The Kubernetes collector inventories every Pod in an exact namespace.  This
adapter deliberately meters only the two workspace Pod shapes already emitted
by ``ContainerProvisioner``; all other Pods remain visible inventory but are an
explicit not-applicable result until later resource slices classify them.

The adapter writes app-DB interval state and immutable LIST shadow diagnostics.
It never writes audit usage events and has no publication/cutover path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any, Literal
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

import asyncpg

from .inventory import (
    InventoryConflictError,
    InventoryContractError,
    InventoryItem,
    ShadowComparisonStatus,
    SnapshotIntervalMutationContext,
    SnapshotObservationContext,
    WatchIntervalMutationContext,
)


_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_WORKSPACE_COMPONENTS = {
    "workspace": ("job", "srw/job-id"),
    "thread-workspace": ("thread", "srw/thread-id"),
}


@dataclass(frozen=True)
class WorkspacePodProjection:
    applies: bool
    reason_code: str
    source_uid: str
    api_version: str
    resource_version: str | None
    namespace: str
    name: str
    accrues: bool
    terminal: bool
    owner_kind: Literal["job", "thread"] | None
    owner_id: UUID | None
    cpu_millicores: int | None
    memory_bytes: int | None
    capacity_quality: str | None
    measurement_algorithm: str | None
    creation_timestamp: datetime | None = None
    start_time: datetime | None = None
    pod_scheduled_transition_time: datetime | None = None
    overhead_cpu_millicores: int = 0
    overhead_memory_bytes: int = 0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _timestamp(value: Any) -> datetime | None:
    """Parse normalized UTC lifecycle evidence without trusting its shape."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _lifecycle_timestamps(
    lifecycle: Mapping[str, Any],
) -> tuple[datetime | None, datetime | None, datetime | None]:
    creation = _timestamp(lifecycle.get("creation_timestamp"))
    start_time = _timestamp(lifecycle.get("start_time"))
    scheduled_condition = _mapping(lifecycle.get("pod_scheduled_condition"))
    scheduled = (
        _timestamp(scheduled_condition.get("last_transition_time"))
        if scheduled_condition.get("status") == "True"
        else None
    )
    return creation, start_time, scheduled


def _sane_lifecycle_evidence(
    projection: WorkspacePodProjection, received_at: datetime
) -> tuple[datetime | None, str | None]:
    """Return the best bounded allocation-start evidence for uncertainty."""

    creation = projection.creation_timestamp
    for candidate, source in (
        (projection.pod_scheduled_transition_time, "pod-scheduled-transition"),
        (projection.start_time, "pod-status-start-time"),
        (creation, "pod-creation-timestamp"),
    ):
        if candidate is None or candidate > received_at:
            continue
        if creation is not None and candidate < creation:
            continue
        return candidate, source
    return None, None


def _microseconds_between(later: datetime, earlier: datetime) -> int:
    delta = later - earlier
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def project_workspace_pod(item: InventoryItem) -> WorkspacePodProjection:
    """Classify one already-normalized Pod without trusting arbitrary fields."""

    payload = _mapping(item.normalized_item)
    labels = _mapping(payload.get("labels"))
    lifecycle = _mapping(payload.get("lifecycle"))
    capacity = _mapping(payload.get("capacity"))
    source_uid = item.source_uid
    api_version = payload.get("api_version")
    resource_version = payload.get("resource_version")
    namespace = payload.get("namespace")
    name = payload.get("name")
    creation_timestamp, start_time, scheduled_transition = _lifecycle_timestamps(
        lifecycle
    )
    if not all(
        isinstance(value, str) and value for value in (api_version, namespace, name)
    ):
        raise InventoryContractError("normalized Pod identity projection is incomplete")
    if resource_version is not None and not isinstance(resource_version, str):
        raise InventoryContractError("normalized Pod resource version is invalid")

    component = labels.get("srw/component")
    shape = _WORKSPACE_COMPONENTS.get(component)
    if labels.get("app") != "srw-workspace" or shape is None:
        return WorkspacePodProjection(
            applies=False,
            reason_code="non-workspace-pod",
            source_uid=source_uid,
            api_version=api_version,
            resource_version=resource_version,
            namespace=namespace,
            name=name,
            accrues=False,
            terminal=bool(lifecycle.get("terminal")),
            owner_kind=None,
            owner_id=None,
            cpu_millicores=None,
            memory_bytes=None,
            capacity_quality=None,
            measurement_algorithm=None,
        )

    owner_kind, label_key = shape
    raw_owner = labels.get(label_key)
    conflicting_key = "srw/thread-id" if owner_kind == "job" else "srw/job-id"
    owner_id: UUID | None = None
    if isinstance(raw_owner, str) and not labels.get(conflicting_key):
        try:
            owner_id = UUID(raw_owner)
        except ValueError:
            pass

    cpu = _nonnegative_int(capacity.get("cpu_millicores"))
    memory = _nonnegative_int(capacity.get("memory_bytes"))
    quality = capacity.get("capacity_quality")
    algorithm = capacity.get("measurement_algorithm") or payload.get(
        "measurement_algorithm"
    )
    if cpu is None or memory is None or not isinstance(quality, str):
        if not item.valid_for_metering:
            return WorkspacePodProjection(
                applies=True,
                reason_code="invalid-observation",
                source_uid=source_uid,
                api_version=api_version,
                resource_version=resource_version,
                namespace=namespace,
                name=name,
                accrues=lifecycle.get("accrues") is True,
                terminal=lifecycle.get("terminal") is True,
                owner_kind=owner_kind,
                owner_id=owner_id,
                cpu_millicores=None,
                memory_bytes=None,
                capacity_quality=None,
                measurement_algorithm=None,
                creation_timestamp=creation_timestamp,
                start_time=start_time,
                pod_scheduled_transition_time=scheduled_transition,
            )
        raise InventoryContractError("valid normalized Pod lacks effective capacity")
    if not isinstance(algorithm, str) or not algorithm:
        raise InventoryContractError("valid normalized Pod lacks measurement algorithm")
    terminal = lifecycle.get("terminal") is True
    accrues = lifecycle.get("accrues") is True and not terminal
    return WorkspacePodProjection(
        applies=True,
        reason_code=("workspace-pod" if owner_id is not None else "invalid-owner"),
        source_uid=source_uid,
        api_version=api_version,
        resource_version=resource_version,
        namespace=namespace,
        name=name,
        accrues=accrues,
        terminal=terminal,
        owner_kind=owner_kind,
        owner_id=owner_id,
        cpu_millicores=cpu,
        memory_bytes=memory,
        capacity_quality=quality,
        measurement_algorithm=algorithm,
        creation_timestamp=creation_timestamp,
        start_time=start_time,
        pod_scheduled_transition_time=scheduled_transition,
        overhead_cpu_millicores=(
            _nonnegative_int(capacity.get("overhead_cpu_millicores")) or 0
        ),
        overhead_memory_bytes=(
            _nonnegative_int(capacity.get("overhead_memory_bytes")) or 0
        ),
    )


@dataclass(frozen=True)
class _Owner:
    kind: Literal["job", "thread"]
    id: UUID
    user_id: UUID
    project_id: UUID | None


@dataclass(frozen=True)
class _IntervalStart:
    started_at: datetime
    source: str
    uncertainty_us: int
    evidence_source: str | None = None


def _timestamp_text(value: datetime) -> str:
    """Render one already-validated UTC instant for immutable diagnostics."""

    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class PodIntervalReconciler:
    """Typed snapshot/WATCH mutation hooks used by ``InventoryStore``."""

    def __init__(self, *, shadow_enabled: bool) -> None:
        self.shadow_enabled = shadow_enabled

    @staticmethod
    async def _resolve_owner(
        conn: asyncpg.Connection, projection: WorkspacePodProjection
    ) -> _Owner | None:
        if projection.owner_kind is None or projection.owner_id is None:
            return None
        if projection.owner_kind == "job":
            query = (
                "SELECT id, user_id, project_id, "
                "context->'workspace_container'->>'pod_name' AS pod_name, "
                "context->'workspace_container'->>'namespace' AS namespace "
                "FROM jobs WHERE id=$1"
            )
        else:
            query = (
                "SELECT id, user_id, project_id, "
                "metadata->'workspace_container'->>'pod_name' AS pod_name, "
                "metadata->'workspace_container'->>'namespace' AS namespace "
                "FROM threads WHERE id=$1"
            )
        row = await conn.fetchrow(
            query,
            projection.owner_id,
        )
        if (
            row is None
            or row["user_id"] is None
            or row["pod_name"] != projection.name
            or row["namespace"] != projection.namespace
        ):
            return None
        return _Owner(
            kind=projection.owner_kind,
            id=projection.owner_id,
            user_id=row["user_id"],
            project_id=row["project_id"],
        )

    @staticmethod
    def _receipt_start(
        projection: WorkspacePodProjection, received_at: datetime
    ) -> _IntervalStart:
        evidence_at, evidence_source = _sane_lifecycle_evidence(projection, received_at)
        uncertainty = (
            0
            if evidence_at is None
            else max(0, _microseconds_between(received_at, evidence_at))
        )
        return _IntervalStart(
            started_at=received_at,
            source="app-db-received",
            uncertainty_us=uncertainty,
            evidence_source=evidence_source,
        )

    @staticmethod
    async def _canonical_start(
        conn: asyncpg.Connection,
        start: _IntervalStart,
    ) -> tuple[_IntervalStart, dict[str, Any] | None]:
        """Clamp a delayed first observation to the durable cutover barrier.

        Resource-interval statements serialize with the cutover control row.  A
        WATCH event that loses that race can still carry a continuously proven
        PodScheduled transition before the barrier.  That lifecycle evidence is
        useful diagnostically, but admitting it as the publication cursor would
        create an interval the post-cutover materializer can never select.
        """

        control = await conn.fetchrow(
            "SELECT cutover_state, cutover_at FROM infra_metering_control "
            "WHERE singleton=TRUE FOR SHARE"
        )
        if control is None:
            raise InventoryConflictError("infrastructure metering control row missing")
        state = str(control["cutover_state"])
        if state == "disabled":
            return start, None
        if state not in {"preparing", "active"}:
            raise InventoryContractError("infrastructure cutover state is invalid")
        cutover_at = control["cutover_at"]
        if (
            not isinstance(cutover_at, datetime)
            or cutover_at.tzinfo is None
            or cutover_at.utcoffset() is None
        ):
            raise InventoryContractError("infrastructure cutover timestamp is invalid")
        cutover_at = cutover_at.astimezone(timezone.utc)
        if start.started_at >= cutover_at:
            return start, None

        diagnostic = {
            "cutover_at": _timestamp_text(cutover_at),
            "observed_started_at": _timestamp_text(start.started_at),
            "observed_start_time_source": start.source,
            "observed_start_uncertainty_us": start.uncertainty_us,
            "observed_start_evidence_source": start.evidence_source,
        }
        return (
            _IntervalStart(
                started_at=cutover_at,
                source="cutover-barrier",
                uncertainty_us=0,
                evidence_source=start.evidence_source,
            ),
            diagnostic,
        )

    @staticmethod
    async def _watch_start(
        conn: asyncpg.Connection,
        context: WatchIntervalMutationContext,
        item: InventoryItem,
        projection: WorkspacePodProjection,
    ) -> _IntervalStart:
        """Backdate only across a transactionally proven continuous absence.

        ADDED may use the last complete LIST's absence proof. MODIFIED may use
        the immediately preceding same-epoch object observation, or the last
        complete LIST item, only when that proof showed the Pod unscheduled.
        Recovery/initial LIST observations never call this path.
        """

        fallback = PodIntervalReconciler._receipt_start(projection, context.received_at)
        transition = projection.pod_scheduled_transition_time
        creation = projection.creation_timestamp
        if (
            transition is None
            or transition > context.received_at
            or (creation is not None and transition < creation)
        ):
            return fallback

        baseline = await conn.fetchrow(
            "SELECT epoch.continuity_health, epoch.continuous_since, "
            "snapshot.id AS snapshot_id, snapshot.received_at AS proof_at, "
            "snapshot.complete, snapshot.manifest_state "
            "FROM resource_inventory_scope_epochs epoch "
            "LEFT JOIN resource_inventory_snapshots snapshot "
            "ON snapshot.id=epoch.last_complete_snapshot_id "
            "AND snapshot.scope_epoch_id=epoch.id "
            "WHERE epoch.id=$1 AND epoch.scope_id=$2 AND epoch.retired_at IS NULL",
            context.scope_epoch_id,
            context.inventory_scope_id,
        )
        if (
            baseline is None
            or baseline["continuity_health"] != "healthy"
            or baseline["continuous_since"] is None
            or baseline["snapshot_id"] is None
            or baseline["complete"] is not True
            or baseline["manifest_state"] not in {"sealed", "items-expired"}
        ):
            return fallback

        prior = await conn.fetchrow(
            "SELECT event_type, received_at, "
            "COALESCE(normalized_item->'lifecycle'->>'accrues', '') "
            "AS prior_accrues "
            "FROM resource_inventory_watch_events "
            "WHERE scope_epoch_id=$1 AND source_kind='pod' AND source_uid=$2 "
            "AND received_at < $3 "
            "ORDER BY received_at DESC, ordinal DESC LIMIT 1",
            context.scope_epoch_id,
            item.source_uid,
            context.received_at,
        )

        proof_at: datetime | None = None
        if context.event_type.value == "added":
            if prior is not None or baseline["manifest_state"] != "sealed":
                return fallback
            present_in_baseline = await conn.fetchval(
                "SELECT TRUE FROM resource_inventory_snapshot_items "
                "WHERE snapshot_id=$1 AND source_kind='pod' AND source_uid=$2",
                baseline["snapshot_id"],
                item.source_uid,
            )
            if not present_in_baseline:
                proof_at = baseline["proof_at"]
        elif context.event_type.value == "modified":
            if prior is not None:
                if (
                    prior["event_type"] not in {"added", "modified"}
                    or prior["prior_accrues"] != "false"
                ):
                    return fallback
                proof_at = prior["received_at"]
            else:
                if baseline["manifest_state"] != "sealed":
                    return fallback
                baseline_item = await conn.fetchrow(
                    "SELECT COALESCE(normalized_item->'lifecycle'->>'accrues', '') "
                    "AS prior_accrues FROM resource_inventory_snapshot_items "
                    "WHERE snapshot_id=$1 AND source_kind='pod' AND source_uid=$2",
                    baseline["snapshot_id"],
                    item.source_uid,
                )
                if baseline_item is None or baseline_item["prior_accrues"] != "false":
                    return fallback
                proof_at = baseline["proof_at"]

        if (
            proof_at is None
            or proof_at < baseline["continuous_since"]
            or transition < proof_at
        ):
            return fallback
        return _IntervalStart(
            started_at=transition,
            source="pod-scheduled-transition",
            uncertainty_us=0,
            evidence_source="continuous-watch-proof",
        )

    @staticmethod
    def _attribution_matches(row: asyncpg.Record, owner: _Owner | None) -> bool:
        if owner is None:
            expected = (
                "unknown",
                None,
                None,
                None,
                None,
                "kubernetes-label-unverified",
                "ambiguous",
            )
        else:
            expected = (
                "customer",
                owner.kind,
                str(owner.id),
                owner.user_id,
                owner.project_id,
                "app-db-owner-binding",
                "exact",
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
        conn: asyncpg.Connection,
        interval_id: UUID,
        received_at: datetime,
    ) -> UUID:
        confirmed = await conn.fetchval(
            "UPDATE resource_intervals SET "
            "last_seen_at=GREATEST(last_seen_at, $2), "
            "last_confirmed_at=GREATEST(last_confirmed_at, $2), "
            "updated_at=statement_timestamp() "
            "WHERE id=$1 AND ended_at IS NULL RETURNING id",
            interval_id,
            received_at,
        )
        if confirmed is None:
            raise InventoryConflictError("workspace Pod confirmation lost its lock")
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
        if not _SAFE_CODE.fullmatch(reason):
            raise InventoryContractError("interval closure reason is invalid")
        closed = await conn.fetchrow(
            "UPDATE resource_intervals SET ended_at=$2, "
            "end_time_source='app-db-received', "
            "end_uncertainty_us=floor(extract(epoch FROM "
            "($2-last_confirmed_at))*1000000)::bigint, end_reason=$3, "
            "updated_at=statement_timestamp() "
            "WHERE id=$1 AND ended_at IS NULL AND $2 >= last_confirmed_at "
            "RETURNING id, source_lifecycle_id",
            interval_id,
            received_at,
            reason,
        )
        if closed is None:
            raise InventoryConflictError("workspace Pod interval closure lost its lock")
        cleared = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET current_interval_id=NULL, "
            "updated_at=statement_timestamp() "
            "WHERE source_lifecycle_id=$1 AND current_interval_id=$2 "
            "RETURNING TRUE",
            closed["source_lifecycle_id"],
            closed["id"],
        )
        if not cleared:
            raise InventoryConflictError(
                "workspace Pod lifecycle head was inconsistent"
            )

    async def _insert_shadow_comparison(
        self,
        conn: asyncpg.Connection,
        *,
        context: SnapshotObservationContext,
        item: InventoryItem,
        projection: WorkspacePodProjection,
        owner: _Owner | None,
    ) -> None:
        if not self.shadow_enabled:
            return
        legacy = None
        if owner is not None:
            legacy_kind = "job" if owner.kind == "job" else "session"
            legacy = await conn.fetchrow(
                "SELECT id, cpu_millicores, mem_bytes, started_at "
                "FROM workspace_intervals "
                "WHERE owner_kind=$1 AND owner_id=$2 AND ended_at IS NULL "
                "FOR SHARE",
                legacy_kind,
                owner.id,
            )

        observed = None
        if context.current_interval_id is not None:
            observed = await conn.fetchrow(
                "SELECT started_at, start_time_source, start_uncertainty_us "
                "FROM resource_intervals WHERE id=$1 AND inventory_scope_id=$2 "
                "AND source_kind='pod' AND source_uid=$3 AND ended_at IS NULL "
                "FOR SHARE",
                context.current_interval_id,
                context.inventory_scope_id,
                projection.source_uid,
            )

        observed_cpu = projection.cpu_millicores if projection.applies else None
        observed_memory = projection.memory_bytes if projection.applies else None
        legacy_started_at = None if legacy is None else legacy["started_at"]
        observed_started_at = None if observed is None else observed["started_at"]
        observed_start_source = (
            None if observed is None else observed["start_time_source"]
        )
        observed_start_uncertainty = (
            None if observed is None else observed["start_uncertainty_us"]
        )
        start_delta_us = (
            None
            if legacy_started_at is None or observed_started_at is None
            else _microseconds_between(observed_started_at, legacy_started_at)
        )
        if not projection.applies or not projection.accrues:
            status = ShadowComparisonStatus.NOT_APPLICABLE
            reason = (
                projection.reason_code
                if not projection.applies
                else "terminal-or-unscheduled"
            )
            explained = True
        elif not item.valid_for_metering:
            status = ShadowComparisonStatus.INVALID_OBSERVATION
            reason = (
                item.item_error.code if item.item_error is not None else "invalid-item"
            )
            explained = False
        elif owner is None:
            status = ShadowComparisonStatus.OWNER_MISMATCH
            reason = "owner-unresolved"
            explained = False
        elif legacy is None:
            status = ShadowComparisonStatus.LEGACY_MISSING
            reason = "legacy-open-missing"
            explained = False
        elif not (
            int(legacy["cpu_millicores"]) == observed_cpu
            and int(legacy["mem_bytes"]) == observed_memory
        ):
            status = ShadowComparisonStatus.CAPACITY_MISMATCH
            cpu_delta = observed_cpu - int(legacy["cpu_millicores"])
            memory_delta = observed_memory - int(legacy["mem_bytes"])
            bounded_start = (
                start_delta_us is not None
                and start_delta_us >= 0
                and observed_start_source == "app-db-received"
                and observed_start_uncertainty is not None
                and start_delta_us <= observed_start_uncertainty
            )
            explained = (
                cpu_delta == projection.overhead_cpu_millicores
                and memory_delta == projection.overhead_memory_bytes
                and (cpu_delta > 0 or memory_delta > 0)
                and (start_delta_us == 0 or bounded_start)
            )
            reason = (
                "admission-overhead-bounded-start"
                if explained and start_delta_us != 0
                else "admission-overhead"
                if explained
                else "capacity-difference"
            )
        elif start_delta_us != 0:
            bounded_start = (
                start_delta_us is not None
                and start_delta_us > 0
                and observed_start_source == "app-db-received"
                and observed_start_uncertainty is not None
                and start_delta_us <= observed_start_uncertainty
            )
            status = ShadowComparisonStatus.LIFETIME_MISMATCH
            reason = (
                "bounded-start-semantics"
                if bounded_start
                else "start-semantics"
                if start_delta_us is not None
                else "start-evidence-missing"
            )
            explained = bounded_start
        else:
            status = ShadowComparisonStatus.MATCHED
            reason = "capacity-and-start-match"
            explained = True

        await conn.execute(
            "INSERT INTO resource_inventory_shadow_comparisons ("
            "snapshot_id, inventory_scope_id, source_uid, owner_kind, owner_id, "
            "owner_trusted, legacy_interval_id, legacy_cpu_millicores, "
            "legacy_memory_bytes, legacy_started_at, observed_cpu_millicores, "
            "observed_memory_bytes, observed_started_at, "
            "observed_start_time_source, observed_start_uncertainty_us, "
            "start_delta_us, status, reason_code, explained, comparison_at) VALUES ("
            "$1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, "
            "$15, $16, $17, $18, $19, $20)",
            context.snapshot_id,
            context.inventory_scope_id,
            projection.source_uid,
            None if owner is None else owner.kind,
            None if owner is None else owner.id,
            owner is not None,
            None if legacy is None else legacy["id"],
            None if legacy is None else legacy["cpu_millicores"],
            None if legacy is None else legacy["mem_bytes"],
            legacy_started_at,
            observed_cpu,
            observed_memory,
            observed_started_at,
            observed_start_source,
            observed_start_uncertainty,
            start_delta_us,
            str(status),
            reason,
            explained,
            context.received_at,
        )

    @staticmethod
    async def _open_interval(
        conn: asyncpg.Connection,
        *,
        inventory_scope_id: UUID,
        source_cluster: str,
        received_at: datetime,
        start: _IntervalStart,
        item: InventoryItem,
        projection: WorkspacePodProjection,
        owner: _Owner | None,
    ) -> UUID:
        start, cutover_clamp = await PodIntervalReconciler._canonical_start(conn, start)
        lifecycle_id = uuid5(
            NAMESPACE_URL,
            f"srw-resource:{source_cluster}:pod:{item.source_uid}",
        )
        await conn.execute(
            "INSERT INTO resource_lifecycle_heads "
            "(source_lifecycle_id, latest_revision_no) VALUES ($1, 0) "
            "ON CONFLICT (source_lifecycle_id) DO NOTHING",
            lifecycle_id,
        )
        revision_no = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET "
            "latest_revision_no=latest_revision_no+1, "
            "updated_at=statement_timestamp() "
            "WHERE source_lifecycle_id=$1 AND current_interval_id IS NULL "
            "RETURNING latest_revision_no",
            lifecycle_id,
        )
        if revision_no is None:
            raise InventoryConflictError("workspace Pod lifecycle head is still open")
        interval_id = uuid4()
        details_payload: dict[str, Any] = {
            "slice": "kubernetes-workspace-shadow-v1",
            "publication_enabled": False,
            "overhead_cpu_millicores": projection.overhead_cpu_millicores,
            "overhead_memory_bytes": projection.overhead_memory_bytes,
        }
        if start.evidence_source is not None:
            details_payload["start_evidence_source"] = start.evidence_source
        if cutover_clamp is not None:
            details_payload["cutover_start_clamp"] = cutover_clamp
        if owner is None:
            details_payload["attribution_diagnostic"] = "owner-unresolved"
        details = json.dumps(
            details_payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        attribution_scope = "customer" if owner is not None else "unknown"
        attribution_source = (
            "app-db-owner-binding"
            if owner is not None
            else "kubernetes-label-unverified"
        )
        attribution_quality = "exact" if owner is not None else "ambiguous"
        await conn.execute(
            "INSERT INTO resource_intervals ("
            "id, inventory_scope_id, source_cluster, source_kind, source_uid, "
            "source_api_version, source_resource_version, source_lifecycle_id, "
            "revision_no, source_revision, namespace, name, category, resource, "
            "measurement_basis, cost_domain, resource_class, attribution_scope, "
            "owner_kind, owner_id, user_id, project_id, attribution_source, "
            "attribution_quality, lifecycle_confidence, cpu_millicores, "
            "memory_bytes, capacity_source, capacity_quality, "
            "measurement_algorithm, started_at, start_time_source, "
            "start_uncertainty_us, last_seen_at, last_confirmed_at, "
            "last_seen_snapshot_id, materialized_through, details) VALUES ("
            "$1,$2,$3,'pod',$4,$5,$6,$7,$8,$9,$10,$11,'compute','workspace_pod',"
            "'scheduler-request','workload-allocation','kubernetes-pod',$12,"
            "$13,$14,$15,$16,$17,$18,'kubernetes-visible',"
            "$19,$20,'pod-effective-request',$21,$22,$23,$24,$25,"
            "$26,$26,$27,$23,$28::jsonb)",
            interval_id,
            inventory_scope_id,
            source_cluster,
            item.source_uid,
            projection.api_version,
            projection.resource_version,
            lifecycle_id,
            revision_no,
            item.revision_hash,
            projection.namespace,
            projection.name,
            attribution_scope,
            None if owner is None else owner.kind,
            None if owner is None else str(owner.id),
            None if owner is None else owner.user_id,
            None if owner is None else owner.project_id,
            attribution_source,
            attribution_quality,
            projection.cpu_millicores,
            projection.memory_bytes,
            projection.capacity_quality,
            projection.measurement_algorithm,
            start.started_at,
            start.source,
            start.uncertainty_us,
            received_at,
            # The interval-scope trigger permits this reference only after the
            # snapshot is sealed. InventoryStore stamps it in _OBSERVE_PRESENT_SQL
            # immediately after sealing, within the same transaction.
            None,
            details,
        )
        linked = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET current_interval_id=$2, "
            "updated_at=statement_timestamp() "
            "WHERE source_lifecycle_id=$1 AND current_interval_id IS NULL "
            "RETURNING TRUE",
            lifecycle_id,
            interval_id,
        )
        if not linked:
            raise InventoryConflictError("workspace Pod interval head link failed")
        return interval_id

    async def _mutate(
        self,
        conn: asyncpg.Connection,
        *,
        inventory_scope_id: UUID,
        source_cluster: str,
        received_at: datetime,
        existing_interval_id: UUID | None,
        item: InventoryItem,
        start: _IntervalStart | None = None,
    ) -> UUID | None:
        projection = project_workspace_pod(item)
        owner = (
            await self._resolve_owner(conn, projection) if projection.applies else None
        )
        if not projection.applies or not projection.accrues:
            reason = (
                "not-applicable"
                if not projection.applies
                else "terminal-or-unscheduled"
            )
            await self._close_existing(
                conn, existing_interval_id, received_at, reason=reason
            )
            return None

        if existing_interval_id is not None:
            existing = await conn.fetchrow(
                "SELECT id, source_revision, attribution_scope, owner_kind, "
                "owner_id, user_id, project_id, attribution_source, "
                "attribution_quality FROM resource_intervals "
                "WHERE id=$1 AND ended_at IS NULL FOR UPDATE",
                existing_interval_id,
            )
            if existing is None:
                raise InventoryConflictError(
                    "workspace Pod current interval disappeared during reconcile"
                )
            same_revision = existing["source_revision"] == item.revision_hash
            if same_revision and self._attribution_matches(existing, owner):
                return await self._confirm_existing(
                    conn, existing_interval_id, received_at
                )
            await self._close_existing(
                conn,
                existing_interval_id,
                received_at,
                reason=("attribution-changed" if same_revision else "revision-changed"),
            )
            # Once an immutable interval has been observed, the precise split
            # boundary is the app-DB receipt that detected the changed facts.
            start = _IntervalStart(
                started_at=received_at,
                source="app-db-received",
                uncertainty_us=0,
                evidence_source="observed-revision-boundary",
            )

        if start is None:
            start = self._receipt_start(projection, received_at)
        return await self._open_interval(
            conn,
            inventory_scope_id=inventory_scope_id,
            source_cluster=source_cluster,
            received_at=received_at,
            start=start,
            item=item,
            projection=projection,
            owner=owner,
        )

    async def apply_snapshot(
        self,
        conn: asyncpg.Connection,
        context: SnapshotIntervalMutationContext,
        item: InventoryItem,
    ) -> UUID | None:
        return await self._mutate(
            conn,
            inventory_scope_id=context.inventory_scope_id,
            source_cluster=context.source_cluster,
            received_at=context.received_at,
            existing_interval_id=context.existing_interval_id,
            item=item,
        )

    async def observe_snapshot(
        self,
        conn: asyncpg.Connection,
        context: SnapshotObservationContext,
        item: InventoryItem,
    ) -> None:
        """Record one immutable diagnostic for every staged Pod identity."""

        projection = project_workspace_pod(item)
        owner = (
            await self._resolve_owner(conn, projection) if projection.applies else None
        )
        await self._insert_shadow_comparison(
            conn,
            context=context,
            item=item,
            projection=projection,
            owner=owner,
        )

    async def apply_watch(
        self,
        conn: asyncpg.Connection,
        context: WatchIntervalMutationContext,
        item: InventoryItem,
    ) -> UUID | None:
        projection = project_workspace_pod(item)
        start = None
        if (
            context.existing_interval_id is None
            and projection.applies
            and projection.accrues
        ):
            start = await self._watch_start(conn, context, item, projection)
        return await self._mutate(
            conn,
            inventory_scope_id=context.inventory_scope_id,
            source_cluster=context.source_cluster,
            received_at=context.received_at,
            existing_interval_id=context.existing_interval_id,
            item=item,
            start=start,
        )


__all__ = [
    "PodIntervalReconciler",
    "WorkspacePodProjection",
    "project_workspace_pod",
]
