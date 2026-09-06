"""Forward-only activation controls for Slice 3 compute product classes.

Agent Pods, on-demand IDE Pods, and VMI compute can be introduced after the
legacy workspace cutover.  Their shadow history must therefore be fenced by
independent database-owned activation boundaries.  This module owns only that
control plane; it does not classify inventory or publish usage events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Mapping
from uuid import UUID

import asyncpg


_UTC = timezone.utc
_ACTIVATION_KEYS = (
    "agent_pod",
    "ide_workspace_pod",
    "workspace_vm",
)
_ACTIVATION_KEY_SET = frozenset(_ACTIVATION_KEYS)
_CLUSTER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_COLLECTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NAMESPACE_RE = re.compile(
    r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)*$"
)
_SCOPE_CONTRACT = {
    "agent_pod": ("kubernetes-pods", "core/v1/pods"),
    "ide_workspace_pod": ("kubernetes-pods", "core/v1/pods"),
    "workspace_vm": ("kubevirt-vmis", "kubevirt.io/v1/virtualmachineinstances"),
}


class ComputeActivationError(RuntimeError):
    """Base class for compute activation failures."""


class ComputeActivationContractError(ComputeActivationError, ValueError):
    """A caller supplied a value outside the activation contract."""


class ComputeActivationConflict(ComputeActivationError):
    """Durable state or shadow evidence conflicts with the requested step."""


class ComputeActivationNotReady(ComputeActivationError):
    """The requested product class has not crossed its activation boundary."""


@dataclass(frozen=True, slots=True)
class ComputeActivation:
    activation_key: str
    state: str
    activated_at: datetime | None
    database_time: datetime | None = None
    authorized_scope_epoch_ids: frozenset[UUID] = frozenset()


@dataclass(frozen=True, slots=True)
class ComputeScopeRequirement:
    activation_key: str
    collector_id: str
    source_cluster: str
    inventory_scope_id: UUID
    inventory_scope_epoch_id: UUID
    api_resource: str
    namespace: str
    required_from: datetime
    epoch_required_for_rollup: bool
    epoch_required_from: datetime | None
    epoch_retired_at: datetime | None


@dataclass(frozen=True, slots=True)
class ComputeEpochAuthority:
    id: UUID
    activation_key: str
    collector_id: str
    source_cluster: str
    inventory_scope_id: UUID
    inventory_scope_epoch_id: UUID
    previous_authority_id: UUID | None
    predecessor_epoch_id: UUID | None
    authority_sequence: int
    effective_from: datetime
    effective_to: datetime | None
    proof_snapshot_id: UUID
    proof_generation: int
    promotion_request_id: UUID
    namespace: str
    is_current_epoch: bool


@dataclass(frozen=True, slots=True)
class ComputeEpochPromotion:
    request_id: UUID
    activation_key: str
    request_kind: str
    promoted_at: datetime
    actor_id: UUID
    audit_reason: str
    replayed: bool
    authorities: tuple[ComputeEpochAuthority, ...]


@dataclass(frozen=True, slots=True)
class ComputeActivationScheduleResult:
    activation: ComputeActivation
    promotion: ComputeEpochPromotion


_ACTIVATION_SQL = """
/* compute-activation:lock */
SELECT activation_key, state, activated_at,
       statement_timestamp() AS database_time
FROM compute_metering_activation
WHERE activation_key = $1
FOR SHARE
"""

_ACTIVATION_UPDATE_SQL = """
/* compute-activation:lock-update */
SELECT activation_key, state, activated_at,
       statement_timestamp() AS database_time
FROM compute_metering_activation
WHERE activation_key = $1
FOR UPDATE
"""


def _activation_key(value: Any) -> str:
    if not isinstance(value, str) or value not in _ACTIVATION_KEY_SET:
        raise ComputeActivationContractError("activation_key is unsupported")
    return value


def _utc(value: Any, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ComputeActivationContractError(f"{name} must be timezone-aware")
    return value.astimezone(_UTC)


def _midnight(value: Any, name: str = "activated_at") -> datetime:
    boundary = _utc(value, name)
    if boundary.hour or boundary.minute or boundary.second or boundary.microsecond:
        raise ComputeActivationContractError(f"{name} must be a UTC midnight")
    return boundary


def _uuid(value: Any, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ComputeActivationContractError(f"{name} must be a UUID") from exc


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ComputeActivationContractError(f"{name} must be positive")
    return value


def _audit_reason(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ComputeActivationContractError(
            "audit_reason must contain 1 to 2048 characters"
        )
    return value


def _promotion_digest(
    *,
    request_kind: str,
    activation_key: str,
    collector_id: str,
    source_cluster: str,
    namespaces: tuple[str, ...],
    expected_generation: int,
    actor_id: UUID,
    audit_reason: str,
    activated_at: datetime | None = None,
) -> str:
    payload = {
        "activated_at": (None if activated_at is None else activated_at.isoformat()),
        "activation_key": activation_key,
        "actor_id": str(actor_id),
        "audit_reason": audit_reason,
        "collector_id": collector_id,
        "expected_generation": expected_generation,
        "namespaces": sorted(namespaces),
        "request_kind": request_kind,
        "source_cluster": source_cluster,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _activation_from_row(row: Mapping[str, Any]) -> ComputeActivation:
    activated_at = row.get("activated_at")
    database_time = row.get("database_time")
    return ComputeActivation(
        activation_key=_activation_key(str(row["activation_key"])),
        state=str(row["state"]),
        activated_at=(
            None if activated_at is None else _utc(activated_at, "activated_at")
        ),
        database_time=(
            None if database_time is None else _utc(database_time, "database_time")
        ),
    )


async def read_compute_activation(
    conn: asyncpg.Connection,
    activation_key: str,
) -> ComputeActivation:
    key = _activation_key(activation_key)
    row = await conn.fetchrow(_ACTIVATION_SQL, key)
    if row is None:
        raise ComputeActivationNotReady("compute activation row is missing")
    return _activation_from_row(row)


async def _read_compute_activation_for_update(
    conn: asyncpg.Connection,
    activation_key: str,
) -> ComputeActivation:
    key = _activation_key(activation_key)
    row = await conn.fetchrow(_ACTIVATION_UPDATE_SQL, key)
    if row is None:
        raise ComputeActivationNotReady("compute activation row is missing")
    return _activation_from_row(row)


async def read_compute_activations(
    conn: asyncpg.Connection,
) -> tuple[ComputeActivation, ...]:
    """Return every fixed product-class activation in stable order."""

    return tuple([await read_compute_activation(conn, key) for key in _ACTIVATION_KEYS])


async def read_compute_scope_requirements(
    conn: asyncpg.Connection,
    activation_key: str | None = None,
) -> tuple[ComputeScopeRequirement, ...]:
    """Read immutable per-class scope/epoch authority in stable order."""

    key = None if activation_key is None else _activation_key(activation_key)
    rows = await conn.fetch(
        "SELECT requirement.activation_key,requirement.collector_id,"
        "requirement.source_cluster,requirement.inventory_scope_id,"
        "requirement.inventory_scope_epoch_id,requirement.required_from,"
        "scope.api_resource,scope.namespace,epoch.required_for_rollup,"
        "epoch.required_from AS epoch_required_from,epoch.retired_at "
        "FROM compute_metering_scope_requirements AS requirement "
        "JOIN resource_inventory_scopes AS scope "
        "ON scope.id=requirement.inventory_scope_id "
        "AND scope.collector_id=requirement.collector_id "
        "AND scope.source_cluster=requirement.source_cluster "
        "JOIN resource_inventory_scope_epochs AS epoch "
        "ON epoch.id=requirement.inventory_scope_epoch_id "
        "AND epoch.scope_id=requirement.inventory_scope_id "
        "WHERE ($1::text IS NULL OR requirement.activation_key=$1) "
        "ORDER BY requirement.activation_key,requirement.inventory_scope_id",
        key,
    )
    requirements: list[ComputeScopeRequirement] = []
    for row in rows:
        namespace = row["namespace"]
        if not isinstance(namespace, str) or not namespace:
            raise ComputeActivationContractError(
                "compute scope requirement namespace is invalid"
            )
        epoch_required_from = row["epoch_required_from"]
        epoch_retired_at = row["retired_at"]
        requirements.append(
            ComputeScopeRequirement(
                activation_key=_activation_key(str(row["activation_key"])),
                collector_id=str(row["collector_id"]),
                source_cluster=str(row["source_cluster"]),
                inventory_scope_id=_uuid(
                    row["inventory_scope_id"], "inventory_scope_id"
                ),
                inventory_scope_epoch_id=_uuid(
                    row["inventory_scope_epoch_id"],
                    "inventory_scope_epoch_id",
                ),
                api_resource=str(row["api_resource"]),
                namespace=namespace,
                required_from=_utc(row["required_from"], "required_from"),
                epoch_required_for_rollup=bool(row["required_for_rollup"]),
                epoch_required_from=(
                    None
                    if epoch_required_from is None
                    else _utc(epoch_required_from, "epoch_required_from")
                ),
                epoch_retired_at=(
                    None
                    if epoch_retired_at is None
                    else _utc(epoch_retired_at, "epoch_retired_at")
                ),
            )
        )
    return tuple(requirements)


_EPOCH_AUTHORITIES_SQL = """
SELECT authority.id, authority.activation_key, authority.collector_id,
       authority.source_cluster, authority.inventory_scope_id,
       authority.inventory_scope_epoch_id, authority.previous_authority_id,
       authority.predecessor_epoch_id, authority.authority_sequence,
       authority.effective_from, epoch.retired_at AS effective_to,
       authority.proof_snapshot_id, authority.proof_generation,
       authority.promotion_request_id, scope.namespace,
       epoch.retired_at IS NULL AS is_current_epoch
FROM compute_metering_epoch_authorities AS authority
JOIN resource_inventory_scopes AS scope
  ON scope.id = authority.inventory_scope_id
 AND scope.collector_id = authority.collector_id
 AND scope.source_cluster = authority.source_cluster
JOIN resource_inventory_scope_epochs AS epoch
  ON epoch.id = authority.inventory_scope_epoch_id
 AND epoch.scope_id = authority.inventory_scope_id
WHERE ($1::text IS NULL OR authority.activation_key = $1)
  AND ($2::uuid IS NULL OR authority.promotion_request_id = $2)
ORDER BY authority.activation_key, scope.namespace,
         authority.authority_sequence, authority.id
"""


def _epoch_authority_from_row(row: Mapping[str, Any]) -> ComputeEpochAuthority:
    namespace = row["namespace"]
    if not isinstance(namespace, str) or not namespace:
        raise ComputeActivationContractError(
            "compute epoch authority namespace is invalid"
        )
    effective_to = row["effective_to"]
    previous = row["previous_authority_id"]
    predecessor = row["predecessor_epoch_id"]
    return ComputeEpochAuthority(
        id=_uuid(row["id"], "authority_id"),
        activation_key=_activation_key(str(row["activation_key"])),
        collector_id=str(row["collector_id"]),
        source_cluster=str(row["source_cluster"]),
        inventory_scope_id=_uuid(row["inventory_scope_id"], "inventory_scope_id"),
        inventory_scope_epoch_id=_uuid(
            row["inventory_scope_epoch_id"], "inventory_scope_epoch_id"
        ),
        previous_authority_id=(
            None if previous is None else _uuid(previous, "previous_authority_id")
        ),
        predecessor_epoch_id=(
            None if predecessor is None else _uuid(predecessor, "predecessor_epoch_id")
        ),
        authority_sequence=_positive_int(
            int(row["authority_sequence"]), "authority_sequence"
        ),
        effective_from=_utc(row["effective_from"], "effective_from"),
        effective_to=(
            None if effective_to is None else _utc(effective_to, "effective_to")
        ),
        proof_snapshot_id=_uuid(row["proof_snapshot_id"], "proof_snapshot_id"),
        proof_generation=_positive_int(
            int(row["proof_generation"]), "proof_generation"
        ),
        promotion_request_id=_uuid(row["promotion_request_id"], "promotion_request_id"),
        namespace=namespace,
        is_current_epoch=bool(row["is_current_epoch"]),
    )


async def read_compute_epoch_authorities(
    conn: asyncpg.Connection,
    activation_key: str | None = None,
    *,
    promotion_request_id: UUID | None = None,
) -> tuple[ComputeEpochAuthority, ...]:
    key = None if activation_key is None else _activation_key(activation_key)
    request_id = (
        None
        if promotion_request_id is None
        else _uuid(promotion_request_id, "promotion_request_id")
    )
    rows = await conn.fetch(_EPOCH_AUTHORITIES_SQL, key, request_id)
    return tuple(_epoch_authority_from_row(row) for row in rows)


def compute_scope_configuration_diagnostic(
    activation: ComputeActivation,
    requirements: tuple[ComputeScopeRequirement, ...],
    *,
    source_cluster: str,
    namespaces: tuple[str, ...],
    collector_id: str | None = None,
    authorities: tuple[ComputeEpochAuthority, ...] = (),
) -> str | None:
    """Return why an active class cannot use its configured exact scope set."""

    key = _activation_key(activation.activation_key)
    default_collector, api_resource = _SCOPE_CONTRACT[key]
    selected_collector = default_collector if collector_id is None else collector_id
    if (
        not isinstance(selected_collector, str)
        or _COLLECTOR_RE.fullmatch(selected_collector) is None
        or not isinstance(source_cluster, str)
        or _CLUSTER_RE.fullmatch(source_cluster) is None
        or not isinstance(namespaces, tuple)
        or not namespaces
        or len(set(namespaces)) != len(namespaces)
        or any(
            not isinstance(namespace, str) or _NAMESPACE_RE.fullmatch(namespace) is None
            for namespace in namespaces
        )
    ):
        return "configured compute scope identity is invalid"

    selected = tuple(
        requirement for requirement in requirements if requirement.activation_key == key
    )
    selected_authorities = tuple(
        authority for authority in authorities if authority.activation_key == key
    )
    if activation.state != "active":
        if selected or selected_authorities:
            return "inactive compute class unexpectedly has frozen scope authority"
        return None
    if activation.activated_at is None:
        return "active compute class has no activation boundary"

    expected = {
        (selected_collector, source_cluster, api_resource, namespace)
        for namespace in namespaces
    }
    actual = {
        (
            requirement.collector_id,
            requirement.source_cluster,
            requirement.api_resource,
            requirement.namespace,
        )
        for requirement in selected
    }
    if actual != expected:
        return "configured compute scope set differs from frozen authority"
    if any(
        requirement.required_from != activation.activated_at
        or not requirement.epoch_required_for_rollup
        or requirement.epoch_required_from is None
        or requirement.epoch_required_from > requirement.required_from
        for requirement in selected
    ):
        return "configured compute scope lacks its immutable activation authority"
    current_authorities = tuple(
        authority for authority in selected_authorities if authority.is_current_epoch
    )
    current_identity = {
        (
            authority.collector_id,
            authority.source_cluster,
            api_resource,
            authority.namespace,
        )
        for authority in current_authorities
    }
    if current_identity != expected or len(current_authorities) != len(expected):
        return "configured compute scope lacks a promoted current recovery epoch"
    if any(
        authority.effective_to is not None
        or authority.effective_from < activation.activated_at
        for authority in current_authorities
    ):
        return "configured compute current epoch authority is inconsistent"
    return None


async def lock_compute_scope_epoch_authority(
    conn: asyncpg.Connection,
    *,
    activation_key: str,
    inventory_scope_id: UUID,
    inventory_scope_epoch_id: UUID,
) -> datetime | None:
    """Return the exact effective boundary or ``None`` after fail-closed drift.

    The immutable requirement must name both the stable scope and the
    reconciliation context's exact current epoch. A successor epoch therefore
    cannot inherit another class's promotion. Snapshot/WATCH ingestion already
    owns its epoch, so the epoch is locked before activation everywhere.
    """

    key = _activation_key(activation_key)
    scope_id = _uuid(inventory_scope_id, "inventory_scope_id")
    epoch_id = _uuid(inventory_scope_epoch_id, "inventory_scope_epoch_id")
    try:
        row = await conn.fetchrow(
            "SELECT authority.effective_from,"
            "epoch.id AS inventory_scope_epoch_id,epoch.retired_at "
            "FROM compute_metering_epoch_authorities AS authority "
            "JOIN resource_inventory_scope_epochs AS epoch "
            "ON epoch.id=authority.inventory_scope_epoch_id "
            "AND epoch.scope_id=authority.inventory_scope_id "
            "WHERE authority.activation_key=$1 "
            "AND authority.inventory_scope_id=$2 "
            "AND authority.inventory_scope_epoch_id=$3 FOR SHARE OF epoch",
            key,
            scope_id,
            epoch_id,
        )
        activation = await read_compute_activation(conn, key)
    except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError):
        return None
    if (
        activation.state != "active"
        or activation.activated_at is None
        or activation.database_time is None
        or row is None
        or _uuid(row["inventory_scope_epoch_id"], "inventory_scope_epoch_id")
        != epoch_id
        or row["retired_at"] is not None
    ):
        return None
    effective_boundary = max(
        activation.activated_at,
        _utc(row["effective_from"], "effective_from"),
    )
    if activation.database_time < effective_boundary:
        return None
    return effective_boundary


async def lock_compute_activation(
    conn: asyncpg.Connection,
    *,
    activation_key: str,
    observed_started_at: datetime,
) -> datetime:
    """Lock an active class and clamp a first interval to its safe boundary."""

    observed = _utc(observed_started_at, "observed_started_at")
    activation = await read_compute_activation(conn, activation_key)
    if (
        activation.state != "active"
        or activation.activated_at is None
        or activation.database_time is None
    ):
        raise ComputeActivationNotReady("compute product class is not active")
    if activation.database_time < activation.activated_at:
        raise ComputeActivationNotReady("compute activation boundary is in the future")
    return max(observed, activation.activated_at)


async def advance_compute_activation_to_shadow(
    conn: asyncpg.Connection,
    activation_key: str,
) -> ComputeActivation:
    """Perform or exactly replay ``disabled -> shadow``."""

    key = _activation_key(activation_key)
    row = await conn.fetchrow(
        "UPDATE compute_metering_activation SET state='shadow' "
        "WHERE activation_key=$1 AND state='disabled' "
        "RETURNING activation_key,state,activated_at,"
        "statement_timestamp() AS database_time",
        key,
    )
    if row is not None:
        return _activation_from_row(row)
    current = await read_compute_activation(conn, key)
    if current.state == "shadow":
        return current
    raise ComputeActivationConflict("compute activation has already passed shadow")


async def schedule_compute_activation(
    conn: asyncpg.Connection,
    *,
    activation_key: str,
    activated_at: datetime,
) -> ComputeActivation:
    """Schedule or exactly replay ``shadow -> active`` at a future UTC day."""

    key = _activation_key(activation_key)
    boundary = _midnight(activated_at)
    # The activation trigger validates every immutable exact requirement. Lock
    # that set first so direct callers use the same epoch -> activation order as
    # InventoryStore reconciliation and ``promote_compute_scope_epochs``.
    await conn.fetch(
        "SELECT epoch.id FROM compute_metering_scope_requirements requirement "
        "JOIN resource_inventory_scope_epochs epoch "
        "ON epoch.id=requirement.inventory_scope_epoch_id "
        "AND epoch.scope_id=requirement.inventory_scope_id "
        "WHERE requirement.activation_key=$1 "
        "ORDER BY epoch.id FOR UPDATE OF epoch",
        key,
    )
    current = await _read_compute_activation_for_update(conn, key)
    if current.database_time is None:
        raise ComputeActivationNotReady("compute activation DB clock is unavailable")
    if current.state == "active":
        if current.activated_at == boundary:
            return current
        raise ComputeActivationConflict(
            "compute product class has another activation boundary"
        )
    if current.state != "shadow":
        raise ComputeActivationConflict(
            "compute product class must enter shadow before activation"
        )
    if boundary <= current.database_time:
        raise ComputeActivationContractError(
            "activated_at must be a future UTC midnight"
        )
    row = await conn.fetchrow(
        "UPDATE compute_metering_activation "
        "SET state='active',activated_at=$2 "
        "WHERE activation_key=$1 AND state='shadow' "
        "RETURNING activation_key,state,activated_at,"
        "statement_timestamp() AS database_time",
        key,
        boundary,
    )
    if row is not None:
        return _activation_from_row(row)
    replay = await read_compute_activation(conn, key)
    if replay.state == "active" and replay.activated_at == boundary:
        return replay
    if replay.state == "active":
        raise ComputeActivationConflict(
            "compute product class has another activation boundary"
        )
    raise ComputeActivationConflict(
        "compute product class must enter shadow before activation"
    )


async def _read_promotion_replay(
    conn: asyncpg.Connection,
    *,
    request_id: UUID,
    activation_key: str,
    request_kind: str,
    collector_id: str,
    source_cluster: str,
    request_digest: str,
) -> ComputeEpochPromotion | None:
    row = await conn.fetchrow(
        "SELECT id,activation_key,request_kind,collector_id,source_cluster,"
        "request_digest,actor_id,audit_reason,promoted_at "
        "FROM compute_metering_epoch_promotion_requests WHERE id=$1",
        request_id,
    )
    if row is None:
        return None
    if (
        row["activation_key"] != activation_key
        or row["request_kind"] != request_kind
        or row["collector_id"] != collector_id
        or row["source_cluster"] != source_cluster
        or row["request_digest"] != request_digest
    ):
        raise ComputeActivationConflict(
            "compute epoch promotion idempotency key has another request"
        )
    authorities = await read_compute_epoch_authorities(
        conn,
        activation_key,
        promotion_request_id=request_id,
    )
    if not authorities:
        raise ComputeActivationConflict(
            "compute epoch promotion request has no authority rows"
        )
    return ComputeEpochPromotion(
        request_id=_uuid(row["id"], "request_id"),
        activation_key=activation_key,
        request_kind=request_kind,
        promoted_at=_utc(row["promoted_at"], "promoted_at"),
        actor_id=_uuid(row["actor_id"], "actor_id"),
        audit_reason=str(row["audit_reason"]),
        replayed=True,
        authorities=authorities,
    )


async def _create_promotion_request(
    conn: asyncpg.Connection,
    *,
    request_id: UUID,
    activation_key: str,
    request_kind: str,
    collector_id: str,
    source_cluster: str,
    request_digest: str,
    actor_id: UUID,
    audit_reason: str,
) -> datetime | None:
    row = await conn.fetchrow(
        "INSERT INTO compute_metering_epoch_promotion_requests ("
        "id,activation_key,request_kind,collector_id,source_cluster,"
        "request_digest,actor_id,audit_reason,promoted_at,created_at) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,"
        "statement_timestamp(),statement_timestamp()) "
        "ON CONFLICT (id) DO NOTHING RETURNING promoted_at",
        request_id,
        activation_key,
        request_kind,
        collector_id,
        source_cluster,
        request_digest,
        actor_id,
        audit_reason,
    )
    return None if row is None else _utc(row["promoted_at"], "promoted_at")


async def promote_compute_scope_epochs(
    conn: asyncpg.Connection,
    *,
    activation_key: str,
    activated_at: datetime,
    source_cluster: str,
    namespaces: tuple[str, ...],
    max_scope_age: timedelta,
    expected_generation: int,
    request_id: UUID,
    actor_id: UUID,
    audit_reason: str,
    collector_id: str | None = None,
) -> ComputeEpochPromotion:
    """Require fresh item-for-item shadow proof and promote matching scopes.

    Agent and IDE product classes intentionally share Pod inventory scopes.
    A scope that is already required from an earlier boundary remains valid;
    this function never moves ``required_from`` forward. The per-class interval
    guard remains the independent no-backfill boundary.
    """

    key = _activation_key(activation_key)
    boundary = _midnight(activated_at)
    if not isinstance(source_cluster, str) or not _CLUSTER_RE.fullmatch(source_cluster):
        raise ComputeActivationContractError("source_cluster is invalid")
    if not isinstance(max_scope_age, timedelta) or max_scope_age <= timedelta(0):
        raise ComputeActivationContractError("max_scope_age must be positive")
    if (
        isinstance(expected_generation, bool)
        or not isinstance(expected_generation, int)
        or expected_generation <= 0
    ):
        raise ComputeActivationContractError("expected_generation must be positive")
    if (
        not isinstance(namespaces, tuple)
        or not namespaces
        or len(set(namespaces)) != len(namespaces)
        or any(
            not isinstance(namespace, str)
            or len(namespace) > 253
            or _NAMESPACE_RE.fullmatch(namespace) is None
            for namespace in namespaces
        )
    ):
        raise ComputeActivationContractError("compute namespaces are invalid")

    default_collector, api_resource = _SCOPE_CONTRACT[key]
    selected_collector = default_collector if collector_id is None else collector_id
    if (
        not isinstance(selected_collector, str)
        or _COLLECTOR_RE.fullmatch(selected_collector) is None
    ):
        raise ComputeActivationContractError("collector_id is invalid")
    promotion_request_id = _uuid(request_id, "request_id")
    promotion_actor_id = _uuid(actor_id, "actor_id")
    reason = _audit_reason(audit_reason)
    request_digest = _promotion_digest(
        request_kind="initial-activation",
        activation_key=key,
        collector_id=selected_collector,
        source_cluster=source_cluster,
        namespaces=namespaces,
        expected_generation=expected_generation,
        actor_id=promotion_actor_id,
        audit_reason=reason,
        activated_at=boundary,
    )
    replay = await _read_promotion_replay(
        conn,
        request_id=promotion_request_id,
        activation_key=key,
        request_kind="initial-activation",
        collector_id=selected_collector,
        source_cluster=source_cluster,
        request_digest=request_digest,
    )
    if replay is not None:
        if {authority.namespace for authority in replay.authorities} != set(
            namespaces
        ) or any(
            authority.effective_from != boundary for authority in replay.authorities
        ):
            raise ComputeActivationConflict(
                "compute activation replay has another exact scope set"
            )
        return replay

    # One scheduler lock order: stable scope identities, exact current epochs,
    # then the activation row. Snapshot/WATCH ingestion already owns its epoch
    # before interval hooks run, so the supported activation API must pre-lock
    # the complete exact epoch set before changing the class state.
    scope_rows = await conn.fetch(
        "SELECT id,namespace FROM resource_inventory_scopes "
        "WHERE collector_id=$1 AND source_cluster=$2 AND api_resource=$3 "
        "AND namespace=ANY($4::text[]) ORDER BY namespace,id FOR SHARE",
        selected_collector,
        source_cluster,
        api_resource,
        list(namespaces),
    )
    expected_namespaces = set(namespaces)
    selected_scope_ids = {
        str(row["namespace"]): _uuid(row["id"], "inventory_scope_id")
        for row in scope_rows
        if row["namespace"] in expected_namespaces
    }
    if set(selected_scope_ids) != expected_namespaces or len(selected_scope_ids) != len(
        expected_namespaces
    ):
        raise ComputeActivationConflict(
            "compute activation lacks an active inventory scope"
        )

    current_generation = await conn.fetchval(
        "SELECT leader_generation FROM infra_metering_control "
        "WHERE singleton=TRUE FOR SHARE"
    )
    if current_generation is None or int(current_generation) != expected_generation:
        raise ComputeActivationConflict(
            "compute activation shadow proof is from another collector generation"
        )
    rows = await conn.fetch(
        "SELECT scope.id AS inventory_scope_id,"
        "epoch.id AS inventory_scope_epoch_id,scope.namespace,"
        "epoch.required_for_rollup,"
        "epoch.required_from,epoch.reliable_from,epoch.continuous_since,"
        "epoch.last_complete_at,epoch.snapshot_health,epoch.item_health,"
        "epoch.continuity_health,epoch.backend_health,epoch.leader_generation,"
        "snapshot.leader_generation AS snapshot_leader_generation,"
        "snapshot.id AS proof_snapshot_id,snapshot.item_count,"
        "snapshot.complete,snapshot.manifest_state,"
        "snapshot.received_at,"
        "(SELECT count(*) FROM compute_shadow_observations observation "
        " WHERE observation.snapshot_id=snapshot.id "
        " AND observation.inventory_scope_id=scope.id "
        " AND observation.activation_key=$4) AS shadow_count,"
        "(SELECT count(*) FROM resource_inventory_snapshot_items item "
        " WHERE item.snapshot_id=snapshot.id AND NOT EXISTS ("
        "   SELECT 1 FROM compute_shadow_observations observation "
        "   WHERE observation.snapshot_id=item.snapshot_id "
        "   AND observation.activation_key=$4 "
        "   AND observation.source_kind=item.source_kind "
        "   AND observation.source_uid=item.source_uid"
        " )) AS missing_shadow_count,"
        "(SELECT count(*) FROM compute_shadow_observations observation "
        " WHERE observation.snapshot_id=snapshot.id "
        " AND observation.activation_key=$4 AND NOT EXISTS ("
        "   SELECT 1 FROM resource_inventory_snapshot_items item "
        "   WHERE item.snapshot_id=observation.snapshot_id "
        "   AND item.source_kind=observation.source_kind "
        "   AND item.source_uid=observation.source_uid"
        " )) AS orphan_shadow_count "
        "FROM resource_inventory_scopes scope "
        "JOIN resource_inventory_scope_epochs epoch ON epoch.scope_id=scope.id "
        "LEFT JOIN resource_inventory_snapshots snapshot "
        "ON snapshot.id=epoch.last_complete_snapshot_id "
        "WHERE scope.collector_id=$1 AND scope.source_cluster=$2 "
        "AND scope.api_resource=$3 AND epoch.retired_at IS NULL "
        "AND scope.id=ANY($5::uuid[]) "
        "ORDER BY epoch.id FOR UPDATE OF epoch",
        selected_collector,
        source_cluster,
        api_resource,
        key,
        list(selected_scope_ids.values()),
    )
    selected = {
        str(row["namespace"]): row
        for row in rows
        if row["namespace"] in expected_namespaces
    }
    if set(selected) != expected_namespaces or len(selected) != len(
        expected_namespaces
    ):
        raise ComputeActivationConflict(
            "compute activation lacks an active inventory scope"
        )

    activation = await _read_compute_activation_for_update(conn, key)
    if activation.state == "active":
        if activation.activated_at != boundary:
            raise ComputeActivationConflict(
                "compute product class has another activation boundary"
            )
    elif activation.state != "shadow":
        raise ComputeActivationConflict(
            "compute product class must enter shadow before activation"
        )

    # Read the immutable set only after waiting for the exact epoch locks; a
    # concurrent exact replay may have committed while this scheduler waited.
    existing_rows = await conn.fetch(
        "SELECT inventory_scope_id,inventory_scope_epoch_id,required_from "
        "FROM compute_metering_scope_requirements WHERE activation_key=$1 "
        "ORDER BY inventory_scope_id FOR SHARE",
        key,
    )

    database_time = _utc(
        await conn.fetchval("SELECT statement_timestamp()"),
        "database_time",
    )

    freshness_floor = database_time - max_scope_age
    for namespace in sorted(expected_namespaces):
        row = selected[namespace]
        if (
            row["reliable_from"] is None
            or row["reliable_from"] > boundary
            or row["continuous_since"] is None
            or row["continuous_since"] > boundary
            or row["last_complete_at"] is None
            or row["last_complete_at"] < freshness_floor
            or row["snapshot_health"] != "healthy"
            or row["item_health"] != "healthy"
            or row["continuity_health"] != "healthy"
            or row["backend_health"] != "healthy"
            or row["leader_generation"] != expected_generation
            or row["snapshot_leader_generation"] != expected_generation
            or row["complete"] is not True
            or row["manifest_state"] != "sealed"
            or row["received_at"] is None
            or int(row["shadow_count"] or 0) != int(row["item_count"] or 0)
            or int(row["missing_shadow_count"] or 0) != 0
            or int(row["orphan_shadow_count"] or 0) != 0
        ):
            raise ComputeActivationConflict(
                "compute activation requires a fresh item-for-item shadow snapshot"
            )

        if row["required_for_rollup"]:
            required_from = row["required_from"]
            if required_from is None or required_from > boundary:
                raise ComputeActivationConflict(
                    "compute inventory scope has a later required boundary"
                )
            continue

        promoted = await conn.fetchval(
            "UPDATE resource_inventory_scope_epochs SET "
            "required_for_rollup=TRUE,required_from=$2,"
            "updated_at=statement_timestamp() WHERE id=$1 "
            "AND retired_at IS NULL AND required_for_rollup=FALSE RETURNING TRUE",
            row["inventory_scope_epoch_id"],
            boundary,
        )
        if not promoted:
            raise ComputeActivationConflict(
                "compute inventory scope changed during activation"
            )

    expected_requirements = {
        (
            _uuid(row["inventory_scope_id"], "inventory_scope_id"),
            _uuid(row["inventory_scope_epoch_id"], "inventory_scope_epoch_id"),
            boundary,
        )
        for row in selected.values()
    }
    existing_requirements = {
        (
            _uuid(row["inventory_scope_id"], "inventory_scope_id"),
            _uuid(row["inventory_scope_epoch_id"], "inventory_scope_epoch_id"),
            _utc(row["required_from"], "required_from"),
        )
        for row in existing_rows
    }
    if existing_requirements:
        if existing_requirements != expected_requirements:
            raise ComputeActivationConflict(
                "compute class has another exact scope epoch set"
            )
        replay = await _read_promotion_replay(
            conn,
            request_id=promotion_request_id,
            activation_key=key,
            request_kind="initial-activation",
            collector_id=selected_collector,
            source_cluster=source_cluster,
            request_digest=request_digest,
        )
        if replay is not None:
            return replay
        raise ComputeActivationConflict(
            "compute class has authority without its promotion request"
        )

    await conn.executemany(
        "INSERT INTO compute_metering_scope_requirements ("
        "activation_key,collector_id,source_cluster,inventory_scope_id,"
        "inventory_scope_epoch_id,required_from) VALUES ($1,$2,$3,$4,$5,$6)",
        [
            (
                key,
                selected_collector,
                source_cluster,
                scope_id,
                epoch_id,
                required_from,
            )
            for scope_id, epoch_id, required_from in sorted(
                expected_requirements,
                key=lambda value: str(value[0]),
            )
        ],
    )

    promoted_at = await _create_promotion_request(
        conn,
        request_id=promotion_request_id,
        activation_key=key,
        request_kind="initial-activation",
        collector_id=selected_collector,
        source_cluster=source_cluster,
        request_digest=request_digest,
        actor_id=promotion_actor_id,
        audit_reason=reason,
    )
    if promoted_at is None:
        replay = await _read_promotion_replay(
            conn,
            request_id=promotion_request_id,
            activation_key=key,
            request_kind="initial-activation",
            collector_id=selected_collector,
            source_cluster=source_cluster,
            request_digest=request_digest,
        )
        if replay is None:
            raise ComputeActivationConflict(
                "compute activation promotion request disappeared"
            )
        return replay

    await conn.executemany(
        "INSERT INTO compute_metering_epoch_authorities ("
        "activation_key,collector_id,source_cluster,inventory_scope_id,"
        "inventory_scope_epoch_id,previous_authority_id,"
        "predecessor_epoch_id,authority_sequence,effective_from,"
        "proof_snapshot_id,proof_generation,promotion_request_id) "
        "VALUES ($1,$2,$3,$4,$5,NULL,NULL,1,$6,$7,$8,$9)",
        [
            (
                key,
                selected_collector,
                source_cluster,
                _uuid(row["inventory_scope_id"], "inventory_scope_id"),
                _uuid(
                    row["inventory_scope_epoch_id"],
                    "inventory_scope_epoch_id",
                ),
                boundary,
                _uuid(row["proof_snapshot_id"], "proof_snapshot_id"),
                expected_generation,
                promotion_request_id,
            )
            for row in sorted(
                selected.values(),
                key=lambda value: str(value["inventory_scope_id"]),
            )
        ],
    )
    authorities = await read_compute_epoch_authorities(
        conn,
        key,
        promotion_request_id=promotion_request_id,
    )
    if len(authorities) != len(expected_namespaces):
        raise ComputeActivationConflict(
            "compute activation authority set is incomplete"
        )
    return ComputeEpochPromotion(
        request_id=promotion_request_id,
        activation_key=key,
        request_kind="initial-activation",
        promoted_at=promoted_at,
        actor_id=promotion_actor_id,
        audit_reason=reason,
        replayed=False,
        authorities=authorities,
    )


async def promote_compute_recovery_epochs(
    conn: asyncpg.Connection,
    *,
    activation_key: str,
    source_cluster: str,
    namespaces: tuple[str, ...],
    max_scope_age: timedelta,
    expected_generation: int,
    request_id: UUID,
    actor_id: UUID,
    audit_reason: str,
    collector_id: str | None = None,
) -> ComputeEpochPromotion:
    """Explicitly promote fresh recovery epochs without bridging their gaps.

    Every stable scope must have a current recovery descendant of its last
    append-only authority, an exact sealed shadow snapshot from the current
    leader generation, and a retired predecessor authority. The database
    promotion timestamp becomes the new interval lower bound; no observation
    received during the unresolved gap can be backfilled.
    """

    key = _activation_key(activation_key)
    if not isinstance(source_cluster, str) or not _CLUSTER_RE.fullmatch(source_cluster):
        raise ComputeActivationContractError("source_cluster is invalid")
    if not isinstance(max_scope_age, timedelta) or max_scope_age <= timedelta(0):
        raise ComputeActivationContractError("max_scope_age must be positive")
    generation = _positive_int(expected_generation, "expected_generation")
    if (
        not isinstance(namespaces, tuple)
        or not namespaces
        or len(set(namespaces)) != len(namespaces)
        or any(
            not isinstance(namespace, str)
            or len(namespace) > 253
            or _NAMESPACE_RE.fullmatch(namespace) is None
            for namespace in namespaces
        )
    ):
        raise ComputeActivationContractError("compute namespaces are invalid")

    default_collector, api_resource = _SCOPE_CONTRACT[key]
    selected_collector = default_collector if collector_id is None else collector_id
    if (
        not isinstance(selected_collector, str)
        or _COLLECTOR_RE.fullmatch(selected_collector) is None
    ):
        raise ComputeActivationContractError("collector_id is invalid")
    promotion_request_id = _uuid(request_id, "request_id")
    promotion_actor_id = _uuid(actor_id, "actor_id")
    reason = _audit_reason(audit_reason)
    request_digest = _promotion_digest(
        request_kind="recovery-rollover",
        activation_key=key,
        collector_id=selected_collector,
        source_cluster=source_cluster,
        namespaces=namespaces,
        expected_generation=generation,
        actor_id=promotion_actor_id,
        audit_reason=reason,
    )
    replay = await _read_promotion_replay(
        conn,
        request_id=promotion_request_id,
        activation_key=key,
        request_kind="recovery-rollover",
        collector_id=selected_collector,
        source_cluster=source_cluster,
        request_digest=request_digest,
    )
    if replay is not None:
        if {authority.namespace for authority in replay.authorities} != set(namespaces):
            raise ComputeActivationConflict(
                "compute rollover replay has another exact scope set"
            )
        return replay

    scope_rows = await conn.fetch(
        "SELECT id,namespace FROM resource_inventory_scopes "
        "WHERE collector_id=$1 AND source_cluster=$2 AND api_resource=$3 "
        "AND namespace=ANY($4::text[]) ORDER BY namespace,id FOR SHARE",
        selected_collector,
        source_cluster,
        api_resource,
        list(namespaces),
    )
    expected_namespaces = set(namespaces)
    selected_scope_ids = {
        str(row["namespace"]): _uuid(row["id"], "inventory_scope_id")
        for row in scope_rows
        if row["namespace"] in expected_namespaces
    }
    if set(selected_scope_ids) != expected_namespaces or len(selected_scope_ids) != len(
        expected_namespaces
    ):
        raise ComputeActivationConflict("compute rollover lacks an inventory scope")

    current_generation = await conn.fetchval(
        "SELECT leader_generation FROM infra_metering_control "
        "WHERE singleton=TRUE FOR SHARE"
    )
    if current_generation is None or int(current_generation) != generation:
        raise ComputeActivationConflict(
            "compute rollover shadow proof is from another collector generation"
        )

    rows = await conn.fetch(
        "SELECT scope.id AS inventory_scope_id,scope.namespace,"
        "epoch.id AS inventory_scope_epoch_id,epoch.recovery_from_epoch_id,"
        "epoch.reliable_from,epoch.continuous_since,epoch.last_complete_at,"
        "epoch.snapshot_health,epoch.item_health,epoch.continuity_health,"
        "epoch.backend_health,epoch.leader_generation,"
        "snapshot.id AS proof_snapshot_id,"
        "snapshot.leader_generation AS snapshot_leader_generation,"
        "snapshot.item_count,snapshot.complete,snapshot.manifest_state,"
        "snapshot.received_at,"
        "(SELECT count(*) FROM compute_shadow_observations observation "
        " WHERE observation.snapshot_id=snapshot.id "
        " AND observation.inventory_scope_id=scope.id "
        " AND observation.activation_key=$4) AS shadow_count,"
        "(SELECT count(*) FROM resource_inventory_snapshot_items item "
        " WHERE item.snapshot_id=snapshot.id AND NOT EXISTS ("
        "   SELECT 1 FROM compute_shadow_observations observation "
        "   WHERE observation.snapshot_id=item.snapshot_id "
        "   AND observation.activation_key=$4 "
        "   AND observation.source_kind=item.source_kind "
        "   AND observation.source_uid=item.source_uid"
        " )) AS missing_shadow_count,"
        "(SELECT count(*) FROM compute_shadow_observations observation "
        " WHERE observation.snapshot_id=snapshot.id "
        " AND observation.activation_key=$4 AND NOT EXISTS ("
        "   SELECT 1 FROM resource_inventory_snapshot_items item "
        "   WHERE item.snapshot_id=observation.snapshot_id "
        "   AND item.source_kind=observation.source_kind "
        "   AND item.source_uid=observation.source_uid"
        " )) AS orphan_shadow_count "
        "FROM resource_inventory_scopes scope "
        "JOIN resource_inventory_scope_epochs epoch ON epoch.scope_id=scope.id "
        "LEFT JOIN resource_inventory_snapshots snapshot "
        "ON snapshot.id=epoch.last_complete_snapshot_id "
        "WHERE scope.collector_id=$1 AND scope.source_cluster=$2 "
        "AND scope.api_resource=$3 AND epoch.retired_at IS NULL "
        "AND scope.id=ANY($5::uuid[]) "
        "ORDER BY epoch.id FOR UPDATE OF epoch",
        selected_collector,
        source_cluster,
        api_resource,
        key,
        list(selected_scope_ids.values()),
    )
    selected = {
        str(row["namespace"]): row
        for row in rows
        if row["namespace"] in expected_namespaces
    }
    if set(selected) != expected_namespaces or len(selected) != len(
        expected_namespaces
    ):
        raise ComputeActivationConflict(
            "compute rollover lacks a current inventory epoch"
        )

    activation = await _read_compute_activation_for_update(conn, key)
    database_time = _utc(
        await conn.fetchval("SELECT statement_timestamp()"),
        "database_time",
    )
    if (
        activation.state != "active"
        or activation.activated_at is None
        or activation.activated_at > database_time
    ):
        raise ComputeActivationConflict(
            "compute product class is not effectively active"
        )

    prior_rows = await conn.fetch(
        "SELECT DISTINCT ON (authority.inventory_scope_id) authority.id,"
        "authority.inventory_scope_id,authority.inventory_scope_epoch_id,"
        "authority.authority_sequence,epoch.retired_at "
        "FROM compute_metering_epoch_authorities authority "
        "JOIN resource_inventory_scope_epochs epoch "
        "ON epoch.id=authority.inventory_scope_epoch_id "
        "AND epoch.scope_id=authority.inventory_scope_id "
        "WHERE authority.activation_key=$1 "
        "AND authority.inventory_scope_id=ANY($2::uuid[]) "
        "ORDER BY authority.inventory_scope_id,authority.authority_sequence DESC",
        key,
        list(selected_scope_ids.values()),
    )
    prior_by_scope = {
        _uuid(row["inventory_scope_id"], "inventory_scope_id"): row
        for row in prior_rows
    }
    if set(prior_by_scope) != set(selected_scope_ids.values()):
        raise ComputeActivationConflict(
            "compute rollover lacks prior exact epoch authority"
        )

    freshness_floor = database_time - max_scope_age
    authority_inputs: list[tuple[Any, ...]] = []
    for namespace in sorted(expected_namespaces):
        row = selected[namespace]
        scope_id = _uuid(row["inventory_scope_id"], "inventory_scope_id")
        epoch_id = _uuid(row["inventory_scope_epoch_id"], "inventory_scope_epoch_id")
        predecessor_id = row["recovery_from_epoch_id"]
        prior = prior_by_scope[scope_id]
        prior_epoch_id = _uuid(
            prior["inventory_scope_epoch_id"],
            "prior_inventory_scope_epoch_id",
        )
        if predecessor_id is None or prior["retired_at"] is None:
            raise ComputeActivationConflict(
                "compute rollover requires a retired recovery predecessor"
            )
        predecessor_uuid = _uuid(predecessor_id, "recovery_from_epoch_id")
        lineage_reaches_prior = await conn.fetchval(
            "WITH RECURSIVE lineage AS ("
            " SELECT id,scope_id,recovery_from_epoch_id,ARRAY[id]::uuid[] AS path,"
            " 1 AS depth FROM resource_inventory_scope_epochs "
            " WHERE id=$1 AND scope_id=$2 UNION ALL "
            " SELECT predecessor.id,predecessor.scope_id,"
            " predecessor.recovery_from_epoch_id,lineage.path||predecessor.id,"
            " lineage.depth+1 FROM lineage "
            " JOIN resource_inventory_scope_epochs predecessor "
            " ON predecessor.id=lineage.recovery_from_epoch_id "
            " AND predecessor.scope_id=$2 WHERE lineage.depth<10000 "
            " AND NOT predecessor.id=ANY(lineage.path)) "
            "SELECT EXISTS (SELECT 1 FROM lineage WHERE id=$3)",
            epoch_id,
            scope_id,
            prior_epoch_id,
        )
        if not lineage_reaches_prior:
            raise ComputeActivationConflict(
                "compute rollover recovery lineage misses prior authority"
            )
        if (
            row["reliable_from"] is None
            or row["reliable_from"] > database_time
            or row["continuous_since"] is None
            or row["continuous_since"] > database_time
            or row["last_complete_at"] is None
            or row["last_complete_at"] < freshness_floor
            or row["snapshot_health"] != "healthy"
            or row["item_health"] != "healthy"
            or row["continuity_health"] != "healthy"
            or row["backend_health"] != "healthy"
            or row["leader_generation"] != generation
            or row["snapshot_leader_generation"] != generation
            or row["complete"] is not True
            or row["manifest_state"] != "sealed"
            or row["received_at"] is None
            or int(row["shadow_count"] or 0) != int(row["item_count"] or 0)
            or int(row["missing_shadow_count"] or 0) != 0
            or int(row["orphan_shadow_count"] or 0) != 0
        ):
            raise ComputeActivationConflict(
                "compute rollover requires a fresh item-for-item shadow snapshot"
            )
        authority_inputs.append(
            (
                key,
                selected_collector,
                source_cluster,
                scope_id,
                epoch_id,
                _uuid(prior["id"], "previous_authority_id"),
                predecessor_uuid,
                int(prior["authority_sequence"]) + 1,
                _uuid(row["proof_snapshot_id"], "proof_snapshot_id"),
                generation,
                promotion_request_id,
            )
        )

    promoted_at = await _create_promotion_request(
        conn,
        request_id=promotion_request_id,
        activation_key=key,
        request_kind="recovery-rollover",
        collector_id=selected_collector,
        source_cluster=source_cluster,
        request_digest=request_digest,
        actor_id=promotion_actor_id,
        audit_reason=reason,
    )
    if promoted_at is None:
        replay = await _read_promotion_replay(
            conn,
            request_id=promotion_request_id,
            activation_key=key,
            request_kind="recovery-rollover",
            collector_id=selected_collector,
            source_cluster=source_cluster,
            request_digest=request_digest,
        )
        if replay is None:
            raise ComputeActivationConflict(
                "compute rollover promotion request disappeared"
            )
        return replay

    await conn.executemany(
        "INSERT INTO compute_metering_epoch_authorities ("
        "activation_key,collector_id,source_cluster,inventory_scope_id,"
        "inventory_scope_epoch_id,previous_authority_id,predecessor_epoch_id,"
        "authority_sequence,effective_from,proof_snapshot_id,proof_generation,"
        "promotion_request_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$12,$9,$10,$11)",
        [values + (promoted_at,) for values in authority_inputs],
    )
    authorities = await read_compute_epoch_authorities(
        conn,
        key,
        promotion_request_id=promotion_request_id,
    )
    if len(authorities) != len(expected_namespaces):
        raise ComputeActivationConflict("compute rollover authority set is incomplete")
    return ComputeEpochPromotion(
        request_id=promotion_request_id,
        activation_key=key,
        request_kind="recovery-rollover",
        promoted_at=promoted_at,
        actor_id=promotion_actor_id,
        audit_reason=reason,
        replayed=False,
        authorities=authorities,
    )


async def confirm_compute_authority_snapshot(
    conn: asyncpg.Connection,
    *,
    activation_keys: tuple[str, ...],
    snapshot_id: UUID,
    scope_epoch_id: UUID,
    inventory_scope_id: UUID,
    received_at: datetime,
) -> tuple[str, ...]:
    """Close authority gaps only after exact post-boundary LIST proof.

    The promotion snapshot predates the authority boundary and cannot prove
    billable lifecycle time. A later complete LIST may bound that unknown time
    only when every staged UID has a class observation and every eligible
    observation owns an open interval bound to this exact epoch and confirmed
    at the LIST receipt.
    """

    keys = tuple(sorted({_activation_key(key) for key in activation_keys}))
    if not keys:
        return ()
    snapshot_uuid = _uuid(snapshot_id, "snapshot_id")
    epoch_uuid = _uuid(scope_epoch_id, "scope_epoch_id")
    scope_uuid = _uuid(inventory_scope_id, "inventory_scope_id")
    receipt = _utc(received_at, "received_at")
    rows = await conn.fetch(
        "WITH confirmed AS ("
        " SELECT authority.activation_key "
        " FROM resource_inventory_snapshots snapshot "
        " JOIN resource_inventory_ingest_tickets ticket "
        " ON ticket.id=snapshot.ingest_ticket_id "
        " AND ticket.scope_epoch_id=snapshot.scope_epoch_id "
        " JOIN infra_metering_control control ON control.singleton=TRUE "
        " JOIN resource_inventory_scope_epochs epoch "
        " ON epoch.id=snapshot.scope_epoch_id "
        " AND epoch.scope_id=snapshot.inventory_scope_id "
        " JOIN compute_metering_epoch_authorities authority "
        " ON authority.inventory_scope_id=snapshot.inventory_scope_id "
        " AND authority.inventory_scope_epoch_id=snapshot.scope_epoch_id "
        " JOIN compute_metering_activation activation "
        " ON activation.activation_key=authority.activation_key "
        " WHERE snapshot.id=$2 AND snapshot.scope_epoch_id=$3 "
        " AND snapshot.inventory_scope_id=$4 "
        " AND snapshot.manifest_state='staging' "
        " AND snapshot.leader_generation=control.leader_generation "
        " AND ticket.leader_generation=control.leader_generation "
        " AND ticket.bound_snapshot_id=snapshot.id "
        " AND ticket.consumed_at IS NULL "
        " AND ticket.expires_at > statement_timestamp() "
        " AND epoch.retired_at IS NULL "
        " AND authority.activation_key=ANY($1::text[]) "
        " AND authority.effective_from < $5 "
        " AND activation.state='active' "
        " AND activation.activated_at IS NOT NULL "
        " AND activation.activated_at <= $5 "
        " AND NOT EXISTS ("
        "   SELECT 1 FROM resource_inventory_snapshot_items item "
        "   WHERE item.snapshot_id=$2 AND NOT EXISTS ("
        "     SELECT 1 FROM compute_shadow_observations observation "
        "     WHERE observation.snapshot_id=item.snapshot_id "
        "     AND observation.inventory_scope_id=$4 "
        "     AND observation.activation_key=authority.activation_key "
        "     AND observation.source_kind=item.source_kind "
        "     AND observation.source_uid=item.source_uid)) "
        " AND NOT EXISTS ("
        "   SELECT 1 FROM compute_shadow_observations observation "
        "   WHERE observation.snapshot_id=$2 "
        "   AND observation.inventory_scope_id=$4 "
        "   AND observation.activation_key=authority.activation_key "
        "   AND NOT EXISTS ("
        "     SELECT 1 FROM resource_inventory_snapshot_items item "
        "     WHERE item.snapshot_id=observation.snapshot_id "
        "     AND item.source_kind=observation.source_kind "
        "     AND item.source_uid=observation.source_uid)) "
        " AND NOT EXISTS ("
        "   SELECT 1 FROM compute_shadow_observations observation "
        "   JOIN resource_inventory_snapshot_items item "
        "   ON item.snapshot_id=observation.snapshot_id "
        "   AND item.source_kind=observation.source_kind "
        "   AND item.source_uid=observation.source_uid "
        "   WHERE observation.snapshot_id=$2 "
        "   AND observation.inventory_scope_id=$4 "
        "   AND observation.activation_key=authority.activation_key "
        "   AND observation.disposition IN ("
        "     'eligible-unpriced','identity-ambiguous') "
        "   AND NOT EXISTS ("
        "     SELECT 1 FROM resource_intervals interval "
        "     WHERE interval.inventory_scope_id=$4 "
        "     AND interval.compute_scope_epoch_id=$3 "
        "     AND interval.source_kind=observation.source_kind "
        "     AND interval.source_uid=observation.source_uid "
        "     AND interval.resource=observation.resource "
        "     AND interval.source_revision=item.revision_hash "
        "     AND interval.cpu_millicores IS NOT DISTINCT FROM "
        "       observation.cpu_millicores "
        "     AND interval.memory_bytes IS NOT DISTINCT FROM "
        "       observation.memory_bytes "
        "     AND interval.attribution_scope=observation.attribution_scope "
        "     AND interval.owner_kind IS NOT DISTINCT FROM "
        "       observation.owner_kind "
        "     AND interval.owner_id IS NOT DISTINCT FROM "
        "       observation.owner_id::text "
        "     AND interval.user_id IS NOT DISTINCT FROM observation.user_id "
        "     AND interval.project_id IS NOT DISTINCT FROM "
        "       observation.project_id "
        "     AND interval.ended_at IS NULL "
        "     AND interval.started_at >= GREATEST("
        "       activation.activated_at,authority.effective_from) "
        "     AND interval.last_confirmed_at >= $5))"
        "), closed AS ("
        " UPDATE resource_inventory_coverage_gaps gap "
        " SET gap_end=$5,updated_at=statement_timestamp() "
        " FROM confirmed "
        " WHERE gap.scope_epoch_id=$3 "
        " AND gap.reason='compute-authority-awaiting-confirmation:' "
        "   || confirmed.activation_key "
        " AND gap.resolution='unresolved' AND gap.gap_end IS NULL "
        " AND gap.gap_start < $5 "
        " RETURNING confirmed.activation_key"
        ") SELECT activation_key FROM closed ORDER BY activation_key",
        list(keys),
        snapshot_uuid,
        epoch_uuid,
        scope_uuid,
        receipt,
    )
    return tuple(str(row["activation_key"]) for row in rows)


class ComputeActivationStore:
    """Transaction wrapper used by future fleet-admin activation routes."""

    def __init__(self, app_pool: asyncpg.Pool) -> None:
        self._app = app_pool

    async def status(self) -> tuple[ComputeActivation, ...]:
        async with self._app.acquire() as conn:
            # ``read_compute_activation`` takes a SHARE row lock so callers
            # see all fixed rows under one activation snapshot. PostgreSQL
            # rejects locking reads in a read-only transaction.
            async with conn.transaction(isolation="repeatable_read"):
                return await read_compute_activations(conn)

    async def read_activations(self) -> tuple[ComputeActivation, ...]:
        return await self.status()

    async def requirements(
        self, activation_key: str | None = None
    ) -> tuple[ComputeScopeRequirement, ...]:
        async with self._app.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read"):
                return await read_compute_scope_requirements(conn, activation_key)

    async def authorities(
        self, activation_key: str | None = None
    ) -> tuple[ComputeEpochAuthority, ...]:
        async with self._app.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read"):
                return await read_compute_epoch_authorities(conn, activation_key)

    async def enter_shadow(self, activation_key: str) -> ComputeActivation:
        async with self._app.acquire() as conn:
            async with conn.transaction():
                return await advance_compute_activation_to_shadow(conn, activation_key)

    async def schedule_activation(
        self,
        *,
        activation_key: str,
        activated_at: datetime,
        source_cluster: str,
        namespaces: tuple[str, ...],
        max_scope_age: timedelta,
        expected_generation: int,
        request_id: UUID,
        actor_id: UUID,
        audit_reason: str,
        collector_id: str | None = None,
    ) -> ComputeActivationScheduleResult:
        async with self._app.acquire() as conn:
            async with conn.transaction():
                promotion = await promote_compute_scope_epochs(
                    conn,
                    activation_key=activation_key,
                    activated_at=activated_at,
                    source_cluster=source_cluster,
                    namespaces=namespaces,
                    max_scope_age=max_scope_age,
                    expected_generation=expected_generation,
                    request_id=request_id,
                    actor_id=actor_id,
                    audit_reason=audit_reason,
                    collector_id=collector_id,
                )
                activation = await schedule_compute_activation(
                    conn,
                    activation_key=activation_key,
                    activated_at=activated_at,
                )
                return ComputeActivationScheduleResult(
                    activation=activation,
                    promotion=promotion,
                )

    async def promote_recovery_epochs(
        self,
        *,
        activation_key: str,
        source_cluster: str,
        namespaces: tuple[str, ...],
        max_scope_age: timedelta,
        expected_generation: int,
        request_id: UUID,
        actor_id: UUID,
        audit_reason: str,
        collector_id: str | None = None,
    ) -> ComputeEpochPromotion:
        async with self._app.acquire() as conn:
            async with conn.transaction():
                return await promote_compute_recovery_epochs(
                    conn,
                    activation_key=activation_key,
                    source_cluster=source_cluster,
                    namespaces=namespaces,
                    max_scope_age=max_scope_age,
                    expected_generation=expected_generation,
                    request_id=request_id,
                    actor_id=actor_id,
                    audit_reason=audit_reason,
                    collector_id=collector_id,
                )


__all__ = [
    "ComputeActivation",
    "ComputeActivationConflict",
    "ComputeActivationContractError",
    "ComputeActivationError",
    "ComputeActivationNotReady",
    "ComputeActivationStore",
    "ComputeActivationScheduleResult",
    "ComputeEpochAuthority",
    "ComputeEpochPromotion",
    "ComputeScopeRequirement",
    "advance_compute_activation_to_shadow",
    "compute_scope_configuration_diagnostic",
    "confirm_compute_authority_snapshot",
    "lock_compute_activation",
    "lock_compute_scope_epoch_authority",
    "promote_compute_recovery_epochs",
    "promote_compute_scope_epochs",
    "read_compute_activation",
    "read_compute_activations",
    "read_compute_epoch_authorities",
    "read_compute_scope_requirements",
    "schedule_compute_activation",
]
