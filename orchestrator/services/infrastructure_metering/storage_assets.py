"""Safe app-DB foundations for physical storage asset lifecycles.

This module deliberately accepts only normalized, opaque volume identities.
Raw CSI handles, CSI attributes, and StorageClass parameters have no API or SQL
column through which they can be persisted. A CSI identity arrives as the
collector's lowercase HMAC digest and is stored unchanged. The weak non-CSI
fallback arrives as the Kubernetes PV UID and is domain-hashed with its cluster
and scheme before storage; the raw UID belongs on the incarnation row.

Shadow classification is intentionally separate from this module's lifecycle
helpers and from ``resource_intervals``.  Storage reconcilers must lock the
activation row and use :func:`lock_storage_activation` to clamp their first
publishable interval to the forward-only activation boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
from typing import Any, Mapping
from uuid import UUID, uuid5

import asyncpg


_UTC = timezone.utc
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_CLUSTER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_CSI_SOURCE_UID_RE = re.compile(r"^[0-9a-f]{64}$")
_CSI_DRIVER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_KEY_FINGERPRINT_CONTEXT = b"srw-infrastructure-volume-identity-key-fingerprint-v1\x00"
_PV_FALLBACK_CONTEXT = b"srw-infrastructure-pv-uid-asset-digest-v1\x00"
_ASSERTION_HASH_CONTEXT = b"srw-storage-backend-destruction-request-v1\x00"
_LIFECYCLE_NAMESPACE = UUID("ea97b917-f523-5bde-b675-271f7038e5d2")


class StorageAssetError(RuntimeError):
    """Base class for durable storage asset failures."""


class StorageAssetContractError(StorageAssetError, ValueError):
    """A caller supplied a value outside the raw-free storage contract."""


class StorageAssetConflict(StorageAssetError):
    """A durable identity or idempotency key already has different intent."""


class StorageAssetNotFound(StorageAssetError):
    """The requested opaque storage asset does not exist."""


class StorageActivationNotReady(StorageAssetError):
    """The requested storage basis is not past its activation boundary."""


@dataclass(frozen=True, slots=True)
class DerivedVolumeAssetIdentity:
    source_cluster: str
    normalized_source_uid: str
    asset_digest: str
    identity_scheme: str
    identity_key_version: str
    csi_driver: str | None
    source_lifecycle_id: UUID


@dataclass(frozen=True, slots=True)
class StorageActivation:
    measurement_basis: str
    state: str
    activated_at: datetime | None
    database_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class VolumeAssetRecord:
    id: UUID
    source_cluster: str
    asset_digest: str
    identity_scheme: str
    identity_key_version: str
    csi_driver: str | None
    source_lifecycle_id: UUID
    lifecycle_state: str
    first_observed_at: datetime
    last_observed_at: datetime
    replayed: bool


@dataclass(frozen=True, slots=True)
class VolumeIncarnationRecord:
    id: UUID
    asset_id: UUID
    pv_uid: str
    detached_at: datetime | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class BackendGapRecord:
    id: UUID
    asset_id: UUID
    gap_start: datetime
    gap_end: datetime | None
    resolution: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class BackendDestructionResult:
    assertion_id: UUID
    idempotency_key: UUID
    asset_id: UUID
    effective_at: datetime
    request_hash: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class BackendUnverifiedAssetRecord:
    asset_id: UUID
    source_cluster: str
    identity_scheme: str
    identity_key_version: str
    csi_driver: str | None
    first_observed_at: datetime
    last_observed_at: datetime
    backend_unverified_at: datetime
    gap_id: UUID
    gap_start: datetime
    reason_code: str
    storage_class_name: str | None
    reclaim_policy: str | None
    backend_deletion_finalizer_observed: bool | None
    volume_mode: str | None
    capacity_bytes: int | None
    detached_at: datetime | None
    detach_reason: str | None


@dataclass(frozen=True, slots=True)
class BackendUnverifiedAssetPage:
    items: tuple[BackendUnverifiedAssetRecord, ...]
    next_cursor: UUID | None


@dataclass(frozen=True, slots=True)
class StorageAssetIncarnationDetail:
    storage_class_name: str | None
    reclaim_policy: str
    backend_deletion_finalizer_observed: bool
    volume_mode: str
    capacity_bytes: int
    first_observed_at: datetime
    last_observed_at: datetime
    detached_at: datetime | None
    detach_reason: str | None


@dataclass(frozen=True, slots=True)
class StorageAssetGapDetail:
    gap_id: UUID
    scope_epoch_id: UUID
    gap_start: datetime
    gap_end: datetime | None
    reason_code: str
    resolution: str
    resolution_assertion_id: UUID | None
    resolved_at: datetime | None


@dataclass(frozen=True, slots=True)
class StorageAssetAssertionDetail:
    assertion_id: UUID
    effective_at: datetime
    evidence_kind: str
    evidence_digest: str
    actor_kind: str
    actor_id: UUID | None
    reason_code: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StorageAssetDetailRecord:
    asset_id: UUID
    source_cluster: str
    identity_scheme: str
    identity_key_version: str
    csi_driver: str | None
    lifecycle_state: str
    first_observed_at: datetime
    last_observed_at: datetime
    backend_unverified_at: datetime | None
    destroyed_at: datetime | None
    incarnations: tuple[StorageAssetIncarnationDetail, ...]
    gaps: tuple[StorageAssetGapDetail, ...]
    assertions: tuple[StorageAssetAssertionDetail, ...]
    history_truncated: bool


_ACTIVATION_SQL = """
/* storage-assets:lock-activation */
SELECT measurement_basis, state, activated_at,
       statement_timestamp() AS database_time
FROM storage_metering_activation
WHERE measurement_basis = $1
FOR SHARE
"""

_INSERT_KEY_SQL = """
/* storage-assets:register-key */
INSERT INTO storage_identity_key_state (
    singleton, key_version, key_fingerprint
) VALUES (TRUE, $1, $2)
ON CONFLICT (singleton) DO NOTHING
RETURNING key_version, key_fingerprint, algorithm, registered_at
"""

_SELECT_KEY_SQL = """
/* storage-assets:read-key */
SELECT key_version, key_fingerprint, algorithm, registered_at
FROM storage_identity_key_state
WHERE singleton = TRUE
FOR SHARE
"""

_SELECT_ASSET_SQL = """
/* storage-assets:lock-asset */
SELECT id, source_cluster, asset_digest, identity_key_version,
       identity_scheme, csi_driver, source_lifecycle_id, lifecycle_state,
       first_observed_at, last_observed_at
FROM storage_volume_assets
WHERE source_cluster = $1 AND asset_digest = $2
FOR UPDATE
"""

_ASSERTION_COLUMNS = """
id, idempotency_key, asset_id, assertion_kind, request_hash, effective_at,
evidence_kind, evidence_digest, actor_kind, actor_id, reason_code, created_at
"""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _text(value: Any, name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise StorageAssetContractError(f"{name} is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise StorageAssetContractError(f"{name} contains control characters")
    return value


def _uuid(value: Any, name: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise StorageAssetContractError(f"{name} must be a UUID") from exc


def _utc(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise StorageAssetContractError(f"{name} must be timezone-aware")
    return value.astimezone(_UTC)


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise StorageAssetContractError(f"{name} must be a lowercase SHA-256 hex")
    return value


def _reason(value: Any, name: str = "reason_code") -> str:
    if not isinstance(value, str) or not _REASON_RE.fullmatch(value):
        raise StorageAssetContractError(f"{name} is invalid")
    return value


def volume_identity_key_fingerprint(identity_key: bytes | str) -> str:
    """Match the collector's domain-separated key-drift fingerprint."""

    if isinstance(identity_key, str):
        key = identity_key.encode("utf-8")
    elif isinstance(identity_key, bytes):
        key = identity_key
    else:
        raise StorageAssetContractError("identity_key must be bytes or text")
    if len(key) < 32:
        raise StorageAssetContractError("identity_key must contain at least 32 bytes")
    return hmac.new(key, _KEY_FINGERPRINT_CONTEXT, hashlib.sha256).hexdigest()


def derive_volume_asset_identity(
    *,
    source_cluster: str,
    normalized_source_uid: str,
    identity_scheme: str,
    identity_key_version: str,
    csi_driver: str | None,
) -> DerivedVolumeAssetIdentity:
    """Turn an opaque collector identity into the only persisted asset digest.

    The CSI branch validates and retains an already-keyed digest. The fallback hashes
    its allowed Kubernetes UID with the stable cluster ID and a distinct
    domain, so that raw UID remains confined to ``storage_volume_incarnations``.
    """

    if not isinstance(source_cluster, str) or not _CLUSTER_RE.fullmatch(source_cluster):
        raise StorageAssetContractError("source_cluster is invalid")
    if not isinstance(identity_key_version, str) or not _KEY_VERSION_RE.fullmatch(
        identity_key_version
    ):
        raise StorageAssetContractError("identity_key_version is invalid")
    normalized_source_uid = _text(
        normalized_source_uid, "normalized_source_uid", maximum=256
    )

    if identity_scheme == "csi-hmac-sha256-v1":
        if _CSI_SOURCE_UID_RE.fullmatch(normalized_source_uid) is None:
            raise StorageAssetContractError("CSI source UID is not an opaque HMAC")
        if not isinstance(csi_driver, str) or not _CSI_DRIVER_RE.fullmatch(csi_driver):
            raise StorageAssetContractError("csi_driver is invalid")
        asset_digest = normalized_source_uid
    elif identity_scheme == "pv-uid-v1":
        _text(normalized_source_uid, "fallback pv_uid", maximum=256)
        if csi_driver is not None:
            raise StorageAssetContractError(
                "fallback identity cannot name a CSI driver"
            )
        asset_digest = hashlib.sha256(
            _PV_FALLBACK_CONTEXT
            + _canonical_json(
                {
                    "identity_scheme": identity_scheme,
                    "source_cluster": source_cluster,
                    "source_uid": normalized_source_uid,
                }
            )
        ).hexdigest()
    else:
        raise StorageAssetContractError("identity_scheme is unsupported")

    lifecycle_id = uuid5(
        _LIFECYCLE_NAMESPACE,
        f"{source_cluster}\x00{asset_digest}",
    )
    return DerivedVolumeAssetIdentity(
        source_cluster=source_cluster,
        normalized_source_uid=normalized_source_uid,
        asset_digest=asset_digest,
        identity_scheme=identity_scheme,
        identity_key_version=identity_key_version,
        csi_driver=csi_driver,
        source_lifecycle_id=lifecycle_id,
    )


async def register_storage_identity_key(
    conn: asyncpg.Connection,
    *,
    key_version: str,
    key_fingerprint: str,
) -> bool:
    """Register the singleton key provenance; return ``True`` on exact replay."""

    if not isinstance(key_version, str) or not _KEY_VERSION_RE.fullmatch(key_version):
        raise StorageAssetContractError("key_version is invalid")
    key_fingerprint = _hash(key_fingerprint, "key_fingerprint")
    inserted = await conn.fetchrow(_INSERT_KEY_SQL, key_version, key_fingerprint)
    row = inserted or await conn.fetchrow(_SELECT_KEY_SQL)
    if row is None:
        raise StorageAssetConflict("storage identity key registration disappeared")
    if (
        str(row["key_version"]) != key_version
        or str(row["key_fingerprint"]) != key_fingerprint
        or str(row["algorithm"]) != "hmac-sha256-v1"
    ):
        raise StorageAssetConflict(
            "storage identity key version or fingerprint changed"
        )
    return inserted is None


async def lock_storage_activation(
    conn: asyncpg.Connection,
    *,
    measurement_basis: str,
    observed_started_at: datetime,
) -> datetime:
    """Lock an active basis and return its publication-safe clamped start."""

    if measurement_basis not in {"claim-requested", "volume-provisioned"}:
        raise StorageAssetContractError("measurement_basis is unsupported")
    observed = _utc(observed_started_at, "observed_started_at")
    row = await conn.fetchrow(_ACTIVATION_SQL, measurement_basis)
    if row is None:
        raise StorageActivationNotReady("storage activation row is missing")
    activation_time = row.get("activated_at")
    database_time = row.get("database_time")
    if (
        str(row.get("state")) != "active"
        or activation_time is None
        or database_time is None
    ):
        raise StorageActivationNotReady("storage basis is not active")
    boundary = _utc(activation_time, "activated_at")
    if _utc(database_time, "database_time") < boundary:
        raise StorageActivationNotReady("storage activation boundary is in the future")
    return max(observed, boundary)


async def read_storage_activation(
    conn: asyncpg.Connection,
    measurement_basis: str,
) -> StorageActivation:
    if measurement_basis not in {"claim-requested", "volume-provisioned"}:
        raise StorageAssetContractError("measurement_basis is unsupported")
    row = await conn.fetchrow(_ACTIVATION_SQL, measurement_basis)
    if row is None:
        raise StorageActivationNotReady("storage activation row is missing")
    return StorageActivation(
        measurement_basis=str(row["measurement_basis"]),
        state=str(row["state"]),
        activated_at=(
            _utc(row["activated_at"], "activated_at")
            if row.get("activated_at") is not None
            else None
        ),
        database_time=_utc(row["database_time"], "database_time"),
    )


async def advance_storage_activation_to_shadow(
    conn: asyncpg.Connection,
    measurement_basis: str,
) -> StorageActivation:
    """Perform or exactly replay the one-way ``disabled -> shadow`` step."""

    row = await conn.fetchrow(
        "UPDATE storage_metering_activation SET state = 'shadow' "
        "WHERE measurement_basis = $1 AND state = 'disabled' "
        "RETURNING measurement_basis, state, activated_at, "
        "statement_timestamp() AS database_time",
        measurement_basis,
    )
    if row is None:
        current = await read_storage_activation(conn, measurement_basis)
        if current.state == "shadow":
            return current
        raise StorageAssetConflict("storage activation has already passed shadow")
    return StorageActivation(
        measurement_basis=str(row["measurement_basis"]),
        state=str(row["state"]),
        activated_at=None,
        database_time=_utc(row["database_time"], "database_time"),
    )


async def schedule_storage_activation(
    conn: asyncpg.Connection,
    *,
    measurement_basis: str,
    activated_at: datetime,
) -> StorageActivation:
    """Schedule or exactly replay ``shadow -> active`` at a future UTC day."""

    boundary = _utc(activated_at, "activated_at")
    if boundary.hour or boundary.minute or boundary.second or boundary.microsecond:
        raise StorageAssetContractError("activated_at must be a UTC midnight")
    current = await conn.fetchrow(
        "SELECT measurement_basis,state,activated_at,"
        "statement_timestamp() AS database_time "
        "FROM storage_metering_activation WHERE measurement_basis=$1",
        measurement_basis,
    )
    if current is None:
        raise StorageActivationNotReady("storage activation row is missing")
    current_state = str(current["state"])
    current_boundary = current.get("activated_at")
    database_time = _utc(current["database_time"], "database_time")
    if current_state == "active":
        if current_boundary == boundary:
            return StorageActivation(
                measurement_basis=measurement_basis,
                state="active",
                activated_at=boundary,
                database_time=database_time,
            )
        raise StorageAssetConflict("storage basis has another activation boundary")
    if current_state != "shadow":
        raise StorageAssetConflict("storage basis must enter shadow before activation")
    if boundary <= database_time:
        raise StorageAssetContractError("activated_at must be a future UTC midnight")
    row = await conn.fetchrow(
        "UPDATE storage_metering_activation "
        "SET state = 'active', activated_at = $2 "
        "WHERE measurement_basis = $1 AND state = 'shadow' "
        "RETURNING measurement_basis, state, activated_at, "
        "statement_timestamp() AS database_time",
        measurement_basis,
        boundary,
    )
    if row is None:
        current = await read_storage_activation(conn, measurement_basis)
        if current.state == "active" and current.activated_at == boundary:
            return current
        if current.state == "active":
            raise StorageAssetConflict("storage basis has another activation boundary")
        raise StorageAssetConflict("storage basis must enter shadow before activation")
    return StorageActivation(
        measurement_basis=str(row["measurement_basis"]),
        state=str(row["state"]),
        activated_at=_utc(row["activated_at"], "activated_at"),
        database_time=_utc(row["database_time"], "database_time"),
    )


async def promote_storage_scope_epochs(
    conn: asyncpg.Connection,
    *,
    measurement_basis: str,
    activated_at: datetime,
    source_cluster: str,
    namespaces: tuple[str, ...],
    max_scope_age: timedelta,
    expected_generation: int,
    identity_key_version: str | None = None,
) -> None:
    """Prove fresh shadow coverage and make the matching scopes required.

    This runs in the same transaction as the one-way activation update. A
    storage basis therefore cannot become active while its sealer/read model
    still treats the inventory scopes as optional zero-coverage inputs.
    """

    boundary = _utc(activated_at, "activated_at")
    if not isinstance(source_cluster, str) or not _CLUSTER_RE.fullmatch(source_cluster):
        raise StorageAssetContractError("source_cluster is invalid")
    if not isinstance(max_scope_age, timedelta) or max_scope_age <= timedelta(0):
        raise StorageAssetContractError("max_scope_age must be positive")
    if (
        isinstance(expected_generation, bool)
        or not isinstance(expected_generation, int)
        or expected_generation <= 0
    ):
        raise StorageAssetContractError("expected_generation must be positive")
    if measurement_basis == "claim-requested":
        api_resource = "core/v1/persistentvolumeclaims"
        expected_namespaces: set[str | None] = set(namespaces)
        if (
            not expected_namespaces
            or None in expected_namespaces
            or len(expected_namespaces) != len(namespaces)
            or any(
                not isinstance(namespace, str) or not namespace or len(namespace) > 253
                for namespace in namespaces
            )
        ):
            raise StorageAssetContractError("claim namespaces are invalid")
    elif measurement_basis == "volume-provisioned":
        api_resource = "core/v1/persistentvolumes"
        expected_namespaces = {None}
        if namespaces:
            raise StorageAssetContractError("volume activation cannot name namespaces")
        if not isinstance(identity_key_version, str) or not _KEY_VERSION_RE.fullmatch(
            identity_key_version
        ):
            raise StorageAssetContractError(
                "volume activation requires its configured identity key version"
            )
        key_registered = await conn.fetchval(
            "SELECT TRUE FROM storage_identity_key_state "
            "WHERE singleton AND algorithm='hmac-sha256-v1' AND key_version=$1",
            identity_key_version,
        )
        if not key_registered:
            raise StorageAssetConflict(
                "volume identity key has not been observed in shadow inventory"
            )
    else:
        raise StorageAssetContractError("measurement_basis is unsupported")

    current_generation = await conn.fetchval(
        "SELECT leader_generation FROM infra_metering_control "
        "WHERE singleton=TRUE FOR SHARE"
    )
    if current_generation is None or int(current_generation) != expected_generation:
        raise StorageAssetConflict(
            "storage activation shadow proof is from another collector generation"
        )

    database_time = _utc(
        await conn.fetchval("SELECT statement_timestamp()"),
        "database_time",
    )
    rows = await conn.fetch(
        "SELECT epoch.id,scope.namespace,epoch.required_for_rollup,"
        "epoch.required_from,epoch.reliable_from,epoch.continuous_since,"
        "epoch.last_complete_at,epoch.snapshot_health,epoch.item_health,"
        "epoch.continuity_health,epoch.backend_health,epoch.leader_generation,"
        "snapshot.leader_generation AS snapshot_leader_generation,"
        "snapshot.item_count,"
        "snapshot.complete,snapshot.manifest_state,snapshot.received_at,"
        "(SELECT count(*) FROM storage_shadow_observations observation "
        " WHERE observation.snapshot_id=snapshot.id "
        " AND observation.inventory_scope_id=scope.id) AS shadow_count "
        "FROM resource_inventory_scopes scope "
        "JOIN resource_inventory_scope_epochs epoch ON epoch.scope_id=scope.id "
        "LEFT JOIN resource_inventory_snapshots snapshot "
        "ON snapshot.id=epoch.last_complete_snapshot_id "
        "WHERE scope.collector_id='kubernetes-pods' "
        "AND scope.source_cluster=$1 AND scope.api_resource=$2 "
        "AND epoch.retired_at IS NULL ORDER BY scope.namespace NULLS FIRST "
        "FOR UPDATE OF epoch",
        source_cluster,
        api_resource,
    )
    selected = {
        row["namespace"]: row for row in rows if row["namespace"] in expected_namespaces
    }
    if set(selected) != expected_namespaces or len(selected) != len(
        expected_namespaces
    ):
        raise StorageAssetConflict("storage activation lacks an active inventory scope")
    freshness_floor = database_time - max_scope_age
    for namespace in sorted(
        expected_namespaces, key=lambda value: "" if value is None else value
    ):
        row = selected[namespace]
        if row["required_for_rollup"]:
            if row["required_from"] != boundary:
                raise StorageAssetConflict(
                    "storage inventory scope has another required boundary"
                )
            continue
        if (
            row["reliable_from"] is None
            or row["continuous_since"] is None
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
        ):
            raise StorageAssetConflict(
                "storage activation requires a fresh continuous shadow snapshot"
            )
        promoted = await conn.fetchval(
            "UPDATE resource_inventory_scope_epochs SET "
            "required_for_rollup=TRUE,required_from=$2,"
            "updated_at=statement_timestamp() WHERE id=$1 "
            "AND retired_at IS NULL AND required_for_rollup=FALSE RETURNING TRUE",
            row["id"],
            boundary,
        )
        if not promoted:
            raise StorageAssetConflict(
                "storage inventory scope changed during activation"
            )


def _asset_record(row: Mapping[str, Any], *, replayed: bool) -> VolumeAssetRecord:
    return VolumeAssetRecord(
        id=_uuid(row["id"], "asset.id"),
        source_cluster=str(row["source_cluster"]),
        asset_digest=str(row["asset_digest"]),
        identity_scheme=str(row["identity_scheme"]),
        identity_key_version=str(row["identity_key_version"]),
        csi_driver=(str(row["csi_driver"]) if row.get("csi_driver") else None),
        source_lifecycle_id=_uuid(
            row["source_lifecycle_id"], "asset.source_lifecycle_id"
        ),
        lifecycle_state=str(row["lifecycle_state"]),
        first_observed_at=_utc(row["first_observed_at"], "first_observed_at"),
        last_observed_at=_utc(row["last_observed_at"], "last_observed_at"),
        replayed=replayed,
    )


async def ensure_volume_asset(
    conn: asyncpg.Connection,
    *,
    identity: DerivedVolumeAssetIdentity,
    observed_at: datetime,
) -> VolumeAssetRecord:
    """Create or confirm one durable asset without ever accepting a raw handle."""

    observed = _utc(observed_at, "observed_at")
    await conn.execute(
        "INSERT INTO resource_lifecycle_heads (source_lifecycle_id) VALUES ($1) "
        "ON CONFLICT (source_lifecycle_id) DO NOTHING",
        identity.source_lifecycle_id,
    )
    inserted = await conn.fetchrow(
        "INSERT INTO storage_volume_assets ("
        "source_cluster, asset_digest, identity_key_version, identity_scheme, "
        "csi_driver, source_lifecycle_id, first_observed_at, last_observed_at"
        ") VALUES ($1, $2, $3, $4, $5, $6, $7, $7) "
        "ON CONFLICT (source_cluster, asset_digest) DO NOTHING "
        "RETURNING id, source_cluster, asset_digest, identity_key_version, "
        "identity_scheme, csi_driver, source_lifecycle_id, lifecycle_state, "
        "first_observed_at, last_observed_at",
        identity.source_cluster,
        identity.asset_digest,
        identity.identity_key_version,
        identity.identity_scheme,
        identity.csi_driver,
        identity.source_lifecycle_id,
        observed,
    )
    row = inserted or await conn.fetchrow(
        _SELECT_ASSET_SQL,
        identity.source_cluster,
        identity.asset_digest,
    )
    if row is None:
        raise StorageAssetConflict("storage volume asset disappeared")
    if (
        str(row["identity_key_version"]) != identity.identity_key_version
        or str(row["identity_scheme"]) != identity.identity_scheme
        or row.get("csi_driver") != identity.csi_driver
        or _uuid(row["source_lifecycle_id"], "source_lifecycle_id")
        != identity.source_lifecycle_id
    ):
        raise StorageAssetConflict("volume asset digest has conflicting provenance")
    if str(row["lifecycle_state"]) == "destroyed":
        raise StorageAssetConflict("destroyed volume asset cannot reappear")
    if inserted is None and observed > _utc(row["last_observed_at"], "last_seen"):
        row = await conn.fetchrow(
            "UPDATE storage_volume_assets SET last_observed_at = $2, "
            "updated_at = statement_timestamp() WHERE id = $1 "
            "RETURNING id, source_cluster, asset_digest, identity_key_version, "
            "identity_scheme, csi_driver, source_lifecycle_id, lifecycle_state, "
            "first_observed_at, last_observed_at",
            row["id"],
            observed,
        )
        if row is None:
            raise StorageAssetConflict("storage volume asset update lost its row")
    return _asset_record(row, replayed=inserted is None)


async def observe_volume_incarnation(
    conn: asyncpg.Connection,
    *,
    asset_id: UUID,
    inventory_scope_id: UUID,
    source_cluster: str,
    pv_uid: str,
    pv_name: str,
    storage_class_name: str | None,
    reclaim_policy: str,
    backend_deletion_finalizer_observed: bool,
    volume_mode: str,
    capacity_bytes: int,
    bound_claim_uid: str | None,
    source_resource_version: str | None,
    observed_at: datetime,
) -> VolumeIncarnationRecord:
    """Create/update a PV incarnation, detaching an older reimport incarnation."""

    asset_id = _uuid(asset_id, "asset_id")
    inventory_scope_id = _uuid(inventory_scope_id, "inventory_scope_id")
    if not _CLUSTER_RE.fullmatch(source_cluster):
        raise StorageAssetContractError("source_cluster is invalid")
    pv_uid = _text(pv_uid, "pv_uid")
    pv_name = _text(pv_name, "pv_name", maximum=253)
    if storage_class_name is not None:
        storage_class_name = _text(
            storage_class_name, "storage_class_name", maximum=253
        )
    if reclaim_policy not in {"delete", "retain", "recycle", "unknown"}:
        raise StorageAssetContractError("reclaim_policy is invalid")
    if not isinstance(backend_deletion_finalizer_observed, bool):
        raise StorageAssetContractError(
            "backend_deletion_finalizer_observed must be boolean"
        )
    if volume_mode not in {"filesystem", "block", "unknown"}:
        raise StorageAssetContractError("volume_mode is invalid")
    if isinstance(capacity_bytes, bool) or not isinstance(capacity_bytes, int):
        raise StorageAssetContractError("capacity_bytes must be an integer")
    if capacity_bytes < 0:
        raise StorageAssetContractError("capacity_bytes cannot be negative")
    if bound_claim_uid is not None:
        bound_claim_uid = _text(bound_claim_uid, "bound_claim_uid")
    if source_resource_version is not None:
        source_resource_version = _text(
            source_resource_version, "source_resource_version", maximum=255
        )
    observed = _utc(observed_at, "observed_at")

    existing = await conn.fetchrow(
        "SELECT id, asset_id, pv_uid, pv_name, storage_class_name, "
        "reclaim_policy, backend_deletion_finalizer_observed, volume_mode, "
        "capacity_bytes, bound_claim_uid, "
        "source_resource_version, last_observed_at, detached_at "
        "FROM storage_volume_incarnations "
        "WHERE source_cluster = $1 AND pv_uid = $2 FOR UPDATE",
        source_cluster,
        pv_uid,
    )
    if existing is not None:
        if _uuid(existing["asset_id"], "incarnation.asset_id") != asset_id:
            raise StorageAssetConflict("PV UID belongs to another volume asset")
        if existing.get("detached_at") is not None:
            raise StorageAssetConflict("detached PV incarnation cannot reopen")
        previous_seen = _utc(existing["last_observed_at"], "last_observed_at")
        if observed < previous_seen:
            raise StorageAssetConflict("PV incarnation observation is out of order")
        if observed == previous_seen and (
            str(existing["pv_name"]) != pv_name
            or existing.get("storage_class_name") != storage_class_name
            or str(existing["reclaim_policy"]) != reclaim_policy
            or bool(existing["backend_deletion_finalizer_observed"])
            is not backend_deletion_finalizer_observed
            or str(existing["volume_mode"]) != volume_mode
            or int(existing["capacity_bytes"]) != capacity_bytes
            or existing.get("bound_claim_uid") != bound_claim_uid
            or existing.get("source_resource_version") != source_resource_version
        ):
            raise StorageAssetConflict(
                "same-time PV incarnation replay changed observed state"
            )
        row = await conn.fetchrow(
            "UPDATE storage_volume_incarnations SET pv_name = $2, "
            "storage_class_name = $3, reclaim_policy = $4, volume_mode = $5, "
            "capacity_bytes = $6, bound_claim_uid = $7, "
            "source_resource_version = $8, "
            "backend_deletion_finalizer_observed = "
            "(backend_deletion_finalizer_observed OR $9), "
            "last_observed_at = GREATEST(last_observed_at, $10), "
            "updated_at = statement_timestamp() WHERE id = $1 "
            "RETURNING id, asset_id, pv_uid, detached_at",
            existing["id"],
            pv_name,
            storage_class_name,
            reclaim_policy,
            volume_mode,
            capacity_bytes,
            bound_claim_uid,
            source_resource_version,
            backend_deletion_finalizer_observed,
            observed,
        )
        assert row is not None
        return VolumeIncarnationRecord(
            id=_uuid(row["id"], "incarnation.id"),
            asset_id=asset_id,
            pv_uid=pv_uid,
            detached_at=None,
            replayed=True,
        )

    await conn.execute(
        "UPDATE storage_volume_incarnations "
        "SET detached_at = GREATEST(last_observed_at, $2), "
        "detach_reason = 'reimported', updated_at = statement_timestamp() "
        "WHERE asset_id = $1 AND detached_at IS NULL",
        asset_id,
        observed,
    )
    row = await conn.fetchrow(
        "INSERT INTO storage_volume_incarnations ("
        "asset_id, inventory_scope_id, source_cluster, pv_uid, pv_name, "
        "storage_class_name, reclaim_policy, "
        "backend_deletion_finalizer_observed, volume_mode, capacity_bytes, "
        "bound_claim_uid, source_resource_version, first_observed_at, "
        "last_observed_at) VALUES ("
        "$1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $13) "
        "RETURNING id, asset_id, pv_uid, detached_at",
        asset_id,
        inventory_scope_id,
        source_cluster,
        pv_uid,
        pv_name,
        storage_class_name,
        reclaim_policy,
        backend_deletion_finalizer_observed,
        volume_mode,
        capacity_bytes,
        bound_claim_uid,
        source_resource_version,
        observed,
    )
    assert row is not None
    return VolumeIncarnationRecord(
        id=_uuid(row["id"], "incarnation.id"),
        asset_id=asset_id,
        pv_uid=pv_uid,
        detached_at=None,
        replayed=False,
    )


async def open_backend_unverified_gap(
    conn: asyncpg.Connection,
    *,
    asset_id: UUID,
    scope_epoch_id: UUID,
    gap_start: datetime,
    reason_code: str,
) -> BackendGapRecord:
    asset_id = _uuid(asset_id, "asset_id")
    scope_epoch_id = _uuid(scope_epoch_id, "scope_epoch_id")
    start = _utc(gap_start, "gap_start")
    reason_code = _reason(reason_code)
    inserted = await conn.fetchrow(
        "INSERT INTO storage_asset_coverage_gaps ("
        "asset_id, scope_epoch_id, gap_start, reason_code"
        ") VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (asset_id) WHERE resolution = 'unresolved' DO NOTHING "
        "RETURNING id, asset_id, scope_epoch_id, gap_start, gap_end, "
        "resolution, reason_code",
        asset_id,
        scope_epoch_id,
        start,
        reason_code,
    )
    row = inserted or await conn.fetchrow(
        "SELECT id, asset_id, scope_epoch_id, gap_start, gap_end, resolution, "
        "reason_code FROM storage_asset_coverage_gaps "
        "WHERE asset_id = $1 AND resolution = 'unresolved' FOR UPDATE",
        asset_id,
    )
    if row is None:
        raise StorageAssetConflict("storage backend gap disappeared")
    if (
        _uuid(row["scope_epoch_id"], "gap.scope_epoch_id") != scope_epoch_id
        or _utc(row["gap_start"], "gap_start") != start
        or str(row["reason_code"]) != reason_code
    ):
        raise StorageAssetConflict("volume asset already has another open gap")
    return BackendGapRecord(
        id=_uuid(row["id"], "gap.id"),
        asset_id=asset_id,
        gap_start=start,
        gap_end=None,
        resolution="unresolved",
        replayed=inserted is None,
    )


async def resolve_backend_gap_reobserved(
    conn: asyncpg.Connection,
    *,
    asset_id: UUID,
    observed_at: datetime,
) -> BackendGapRecord:
    asset_id = _uuid(asset_id, "asset_id")
    observed = _utc(observed_at, "observed_at")
    row = await conn.fetchrow(
        "SELECT id, asset_id, gap_start, gap_end, resolution "
        "FROM storage_asset_coverage_gaps WHERE asset_id = $1 "
        "ORDER BY gap_start DESC LIMIT 1 FOR UPDATE",
        asset_id,
    )
    if row is None:
        raise StorageAssetConflict("volume asset has no backend gap")
    if str(row["resolution"]) == "reobserved":
        if _utc(row["gap_end"], "gap_end") != observed:
            raise StorageAssetConflict("backend gap has another resolution boundary")
        replayed = True
    elif str(row["resolution"]) == "unresolved":
        if observed < _utc(row["gap_start"], "gap_start"):
            raise StorageAssetContractError("re-observation predates the gap")
        updated = await conn.fetchrow(
            "UPDATE storage_asset_coverage_gaps SET gap_end = $2, "
            "resolution = 'reobserved', resolved_at = statement_timestamp(), "
            "updated_at = statement_timestamp() "
            "WHERE id = $1 AND resolution = 'unresolved' "
            "RETURNING id, asset_id, gap_start, gap_end, resolution",
            row["id"],
            observed,
        )
        if updated is None:
            raise StorageAssetConflict("backend gap changed concurrently")
        row = updated
        replayed = False
    else:
        raise StorageAssetConflict("backend gap was resolved by destruction")
    return BackendGapRecord(
        id=_uuid(row["id"], "gap.id"),
        asset_id=asset_id,
        gap_start=_utc(row["gap_start"], "gap_start"),
        gap_end=_utc(row["gap_end"], "gap_end"),
        resolution="reobserved",
        replayed=replayed,
    )


def backend_destruction_request_hash(
    *,
    asset_id: UUID,
    effective_at: datetime,
    evidence_kind: str,
    evidence_digest: str,
    actor_kind: str,
    actor_id: UUID | None,
    reason_code: str,
) -> str:
    payload = {
        "actor_id": str(actor_id) if actor_id is not None else None,
        "actor_kind": actor_kind,
        "asset_id": str(_uuid(asset_id, "asset_id")),
        "effective_at": _utc(effective_at, "effective_at")
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "evidence_digest": _hash(evidence_digest, "evidence_digest"),
        "evidence_kind": evidence_kind,
        "reason_code": _reason(reason_code),
    }
    return hashlib.sha256(
        _ASSERTION_HASH_CONTEXT + _canonical_json(payload)
    ).hexdigest()


async def assert_backend_destroyed(
    conn: asyncpg.Connection,
    *,
    idempotency_key: UUID,
    asset_id: UUID,
    effective_at: datetime,
    evidence_kind: str,
    evidence_digest: str,
    actor_kind: str,
    actor_id: UUID | None,
    reason_code: str,
) -> BackendDestructionResult:
    """Append or exactly replay one backend-destruction assertion."""

    idempotency_key = _uuid(idempotency_key, "idempotency_key")
    asset_id = _uuid(asset_id, "asset_id")
    effective_at = _utc(effective_at, "effective_at")
    if evidence_kind not in {
        "csi-confirmed",
        "provider-confirmed",
        "delete-finalizer-confirmed",
        "operator-attested",
    }:
        raise StorageAssetContractError("evidence_kind is unsupported")
    evidence_digest = _hash(evidence_digest, "evidence_digest")
    if actor_kind == "user":
        actor_id = _uuid(actor_id, "actor_id")
    elif actor_kind == "service":
        if actor_id is not None:
            raise StorageAssetContractError("service assertion cannot name actor_id")
    else:
        raise StorageAssetContractError("actor_kind is unsupported")
    reason_code = _reason(reason_code)
    request_hash = backend_destruction_request_hash(
        asset_id=asset_id,
        effective_at=effective_at,
        evidence_kind=evidence_kind,
        evidence_digest=evidence_digest,
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason_code=reason_code,
    )
    # Serialize by durable asset before checking idempotency. Two identical
    # operator requests can then never race past the pre-insert lookup: the
    # follower observes the committed assertion and returns an exact replay.
    asset = await conn.fetchrow(
        "SELECT asset.id,asset.lifecycle_state,asset.source_lifecycle_id,"
        "EXISTS (SELECT 1 FROM storage_asset_coverage_gaps gap "
        "WHERE gap.asset_id=asset.id AND gap.resolution='unresolved') AS open_gap,"
        "EXISTS (SELECT 1 FROM storage_volume_incarnations incarnation "
        "WHERE incarnation.asset_id=asset.id AND incarnation.detached_at IS NULL) "
        "AS active_incarnation,"
        "EXISTS (SELECT 1 FROM resource_intervals interval "
        "WHERE interval.source_lifecycle_id=asset.source_lifecycle_id "
        "AND interval.ended_at IS NULL) AS open_interval "
        "FROM storage_volume_assets asset WHERE asset.id=$1 FOR UPDATE",
        asset_id,
    )
    if asset is None:
        raise StorageAssetNotFound("storage asset not found")
    prior = await conn.fetchrow(
        f"SELECT {_ASSERTION_COLUMNS} FROM storage_backend_assertions "
        "WHERE idempotency_key = $1 OR asset_id = $2 "
        "ORDER BY (idempotency_key = $1) DESC LIMIT 1 FOR SHARE",
        idempotency_key,
        asset_id,
    )
    if prior is not None:
        if (
            _uuid(prior["idempotency_key"], "assertion.idempotency_key")
            == idempotency_key
            and _uuid(prior["asset_id"], "assertion.asset_id") == asset_id
            and str(prior["request_hash"]) == request_hash
        ):
            return BackendDestructionResult(
                assertion_id=_uuid(prior["id"], "assertion.id"),
                idempotency_key=idempotency_key,
                asset_id=asset_id,
                effective_at=effective_at,
                request_hash=request_hash,
                replayed=True,
            )
        raise StorageAssetConflict(
            "storage asset or idempotency key already has another assertion"
        )
    if (
        asset["lifecycle_state"] != "backend-unverified"
        or asset["open_gap"] is not True
        or asset["active_incarnation"] is True
        or asset["open_interval"] is True
    ):
        raise StorageAssetConflict(
            "storage asset is not detached with one backend-unverified gap"
        )
    inserted = await conn.fetchrow(
        f"INSERT INTO storage_backend_assertions ("
        "idempotency_key, asset_id, request_hash, effective_at, evidence_kind, "
        "evidence_digest, actor_kind, actor_id, reason_code) VALUES ("
        "$1, $2, $3, $4, $5, $6, $7, $8, $9) "
        "ON CONFLICT DO NOTHING RETURNING "
        f"{_ASSERTION_COLUMNS}",
        idempotency_key,
        asset_id,
        request_hash,
        effective_at,
        evidence_kind,
        evidence_digest,
        actor_kind,
        actor_id,
        reason_code,
    )
    row = inserted or await conn.fetchrow(
        f"SELECT {_ASSERTION_COLUMNS} FROM storage_backend_assertions "
        "WHERE idempotency_key = $1",
        idempotency_key,
    )
    if row is None:
        raise StorageAssetConflict("storage backend assertion disappeared")
    if (
        _uuid(row["asset_id"], "assertion.asset_id") != asset_id
        or str(row["request_hash"]) != request_hash
    ):
        raise StorageAssetConflict(
            "storage assertion idempotency replay changed immutable intent"
        )
    return BackendDestructionResult(
        assertion_id=_uuid(row["id"], "assertion.id"),
        idempotency_key=idempotency_key,
        asset_id=asset_id,
        effective_at=effective_at,
        request_hash=request_hash,
        replayed=inserted is None,
    )


async def list_backend_unverified_assets(
    conn: asyncpg.Connection,
    *,
    limit: int = 100,
    after_asset_id: UUID | None = None,
) -> BackendUnverifiedAssetPage:
    """Return a bounded, raw-handle-free operator work queue."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise StorageAssetContractError("limit must be between 1 and 200")
    cursor = None if after_asset_id is None else _uuid(after_asset_id, "after_asset_id")
    rows = await conn.fetch(
        "WITH candidates AS ("
        " SELECT asset.* FROM storage_volume_assets asset "
        " WHERE asset.lifecycle_state='backend-unverified' "
        " AND ($1::uuid IS NULL OR (asset.backend_unverified_at,asset.id) > ("
        "  SELECT marker.backend_unverified_at,marker.id "
        "  FROM storage_volume_assets marker WHERE marker.id=$1"
        " )) ORDER BY asset.backend_unverified_at,asset.id LIMIT $2"
        ") SELECT asset.id AS asset_id,asset.source_cluster,"
        "asset.identity_scheme,asset.identity_key_version,asset.csi_driver,"
        "asset.first_observed_at,asset.last_observed_at,"
        "asset.backend_unverified_at,gap.id AS gap_id,gap.gap_start,"
        "gap.reason_code,incarnation.storage_class_name,"
        "incarnation.reclaim_policy,"
        "incarnation.backend_deletion_finalizer_observed,"
        "incarnation.volume_mode,incarnation.capacity_bytes,"
        "incarnation.detached_at,incarnation.detach_reason "
        "FROM candidates asset "
        "JOIN storage_asset_coverage_gaps gap ON gap.asset_id=asset.id "
        "AND gap.resolution='unresolved' "
        "LEFT JOIN LATERAL ("
        " SELECT storage_class_name,reclaim_policy,"
        " backend_deletion_finalizer_observed,volume_mode,capacity_bytes,"
        " detached_at,detach_reason FROM storage_volume_incarnations "
        " WHERE asset_id=asset.id ORDER BY first_observed_at DESC,id DESC LIMIT 1"
        ") incarnation ON TRUE ORDER BY asset.backend_unverified_at,asset.id",
        cursor,
        limit + 1,
    )
    has_more = len(rows) > limit
    visible = rows[:limit]
    items = tuple(
        BackendUnverifiedAssetRecord(
            asset_id=_uuid(row["asset_id"], "asset_id"),
            source_cluster=str(row["source_cluster"]),
            identity_scheme=str(row["identity_scheme"]),
            identity_key_version=str(row["identity_key_version"]),
            csi_driver=(None if row["csi_driver"] is None else str(row["csi_driver"])),
            first_observed_at=_utc(row["first_observed_at"], "first_observed_at"),
            last_observed_at=_utc(row["last_observed_at"], "last_observed_at"),
            backend_unverified_at=_utc(
                row["backend_unverified_at"], "backend_unverified_at"
            ),
            gap_id=_uuid(row["gap_id"], "gap_id"),
            gap_start=_utc(row["gap_start"], "gap_start"),
            reason_code=str(row["reason_code"]),
            storage_class_name=(
                None
                if row["storage_class_name"] is None
                else str(row["storage_class_name"])
            ),
            reclaim_policy=(
                None if row["reclaim_policy"] is None else str(row["reclaim_policy"])
            ),
            backend_deletion_finalizer_observed=(
                None
                if row["backend_deletion_finalizer_observed"] is None
                else bool(row["backend_deletion_finalizer_observed"])
            ),
            volume_mode=(
                None if row["volume_mode"] is None else str(row["volume_mode"])
            ),
            capacity_bytes=(
                None if row["capacity_bytes"] is None else int(row["capacity_bytes"])
            ),
            detached_at=(
                None
                if row["detached_at"] is None
                else _utc(row["detached_at"], "detached_at")
            ),
            detach_reason=(
                None if row["detach_reason"] is None else str(row["detach_reason"])
            ),
        )
        for row in visible
    )
    return BackendUnverifiedAssetPage(
        items=items,
        next_cursor=(items[-1].asset_id if has_more and items else None),
    )


async def read_storage_asset_detail(
    conn: asyncpg.Connection,
    *,
    asset_id: UUID,
    history_limit: int = 100,
) -> StorageAssetDetailRecord:
    """Read one safe operator projection without raw backend identifiers."""

    asset_id = _uuid(asset_id, "asset_id")
    if (
        isinstance(history_limit, bool)
        or not isinstance(history_limit, int)
        or not 1 <= history_limit <= 200
    ):
        raise StorageAssetContractError("history_limit must be between 1 and 200")
    asset = await conn.fetchrow(
        "SELECT id,source_cluster,identity_scheme,identity_key_version,"
        "csi_driver,lifecycle_state,first_observed_at,last_observed_at,"
        "backend_unverified_at,destroyed_at FROM storage_volume_assets "
        "WHERE id=$1",
        asset_id,
    )
    if asset is None:
        raise StorageAssetNotFound("storage asset not found")
    incarnation_rows = await conn.fetch(
        "SELECT storage_class_name,reclaim_policy,"
        "backend_deletion_finalizer_observed,volume_mode,capacity_bytes,"
        "first_observed_at,last_observed_at,detached_at,detach_reason "
        "FROM storage_volume_incarnations WHERE asset_id=$1 "
        "ORDER BY first_observed_at DESC,id DESC LIMIT $2",
        asset_id,
        history_limit + 1,
    )
    gap_rows = await conn.fetch(
        "SELECT id,scope_epoch_id,gap_start,gap_end,reason_code,resolution,"
        "resolution_assertion_id,resolved_at FROM storage_asset_coverage_gaps "
        "WHERE asset_id=$1 ORDER BY gap_start DESC,id DESC LIMIT $2",
        asset_id,
        history_limit + 1,
    )
    assertion_rows = await conn.fetch(
        "SELECT id,effective_at,evidence_kind,evidence_digest,actor_kind,"
        "actor_id,reason_code,created_at FROM storage_backend_assertions "
        "WHERE asset_id=$1 ORDER BY created_at DESC,id DESC LIMIT $2",
        asset_id,
        history_limit + 1,
    )
    truncated = any(
        len(rows) > history_limit
        for rows in (incarnation_rows, gap_rows, assertion_rows)
    )
    incarnations = tuple(
        StorageAssetIncarnationDetail(
            storage_class_name=(
                None
                if row["storage_class_name"] is None
                else str(row["storage_class_name"])
            ),
            reclaim_policy=str(row["reclaim_policy"]),
            backend_deletion_finalizer_observed=bool(
                row["backend_deletion_finalizer_observed"]
            ),
            volume_mode=str(row["volume_mode"]),
            capacity_bytes=int(row["capacity_bytes"]),
            first_observed_at=_utc(row["first_observed_at"], "first_observed_at"),
            last_observed_at=_utc(row["last_observed_at"], "last_observed_at"),
            detached_at=(
                None
                if row["detached_at"] is None
                else _utc(row["detached_at"], "detached_at")
            ),
            detach_reason=(
                None if row["detach_reason"] is None else str(row["detach_reason"])
            ),
        )
        for row in incarnation_rows[:history_limit]
    )
    gaps = tuple(
        StorageAssetGapDetail(
            gap_id=_uuid(row["id"], "gap_id"),
            scope_epoch_id=_uuid(row["scope_epoch_id"], "scope_epoch_id"),
            gap_start=_utc(row["gap_start"], "gap_start"),
            gap_end=(
                None if row["gap_end"] is None else _utc(row["gap_end"], "gap_end")
            ),
            reason_code=str(row["reason_code"]),
            resolution=str(row["resolution"]),
            resolution_assertion_id=(
                None
                if row["resolution_assertion_id"] is None
                else _uuid(row["resolution_assertion_id"], "resolution_assertion_id")
            ),
            resolved_at=(
                None
                if row["resolved_at"] is None
                else _utc(row["resolved_at"], "resolved_at")
            ),
        )
        for row in gap_rows[:history_limit]
    )
    assertions = tuple(
        StorageAssetAssertionDetail(
            assertion_id=_uuid(row["id"], "assertion_id"),
            effective_at=_utc(row["effective_at"], "effective_at"),
            evidence_kind=str(row["evidence_kind"]),
            evidence_digest=str(row["evidence_digest"]),
            actor_kind=str(row["actor_kind"]),
            actor_id=(
                None if row["actor_id"] is None else _uuid(row["actor_id"], "actor_id")
            ),
            reason_code=str(row["reason_code"]),
            created_at=_utc(row["created_at"], "created_at"),
        )
        for row in assertion_rows[:history_limit]
    )
    return StorageAssetDetailRecord(
        asset_id=_uuid(asset["id"], "asset_id"),
        source_cluster=str(asset["source_cluster"]),
        identity_scheme=str(asset["identity_scheme"]),
        identity_key_version=str(asset["identity_key_version"]),
        csi_driver=(None if asset["csi_driver"] is None else str(asset["csi_driver"])),
        lifecycle_state=str(asset["lifecycle_state"]),
        first_observed_at=_utc(asset["first_observed_at"], "first_observed_at"),
        last_observed_at=_utc(asset["last_observed_at"], "last_observed_at"),
        backend_unverified_at=(
            None
            if asset["backend_unverified_at"] is None
            else _utc(asset["backend_unverified_at"], "backend_unverified_at")
        ),
        destroyed_at=(
            None
            if asset["destroyed_at"] is None
            else _utc(asset["destroyed_at"], "destroyed_at")
        ),
        incarnations=incarnations,
        gaps=gaps,
        assertions=assertions,
        history_truncated=truncated,
    )


class StorageAssetStore:
    """Small transaction wrapper for callers that do not already own one."""

    def __init__(self, app_pool: asyncpg.Pool) -> None:
        self._app = app_pool

    async def register_identity_key(
        self, *, key_version: str, key_fingerprint: str
    ) -> bool:
        async with self._app.acquire() as conn:
            async with conn.transaction():
                return await register_storage_identity_key(
                    conn,
                    key_version=key_version,
                    key_fingerprint=key_fingerprint,
                )

    async def read_activations(self) -> tuple[StorageActivation, StorageActivation]:
        """Return claim then volume activation state under one DB snapshot."""

        async with self._app.acquire() as conn:
            async with conn.transaction():
                claim = await read_storage_activation(conn, "claim-requested")
                volume = await read_storage_activation(conn, "volume-provisioned")
                return claim, volume

    async def enter_shadow(self, measurement_basis: str) -> StorageActivation:
        async with self._app.acquire() as conn:
            async with conn.transaction():
                return await advance_storage_activation_to_shadow(
                    conn, measurement_basis
                )

    async def schedule_activation(
        self,
        *,
        measurement_basis: str,
        activated_at: datetime,
        source_cluster: str,
        namespaces: tuple[str, ...],
        max_scope_age: timedelta,
        expected_generation: int,
        identity_key_version: str | None = None,
    ) -> StorageActivation:
        async with self._app.acquire() as conn:
            async with conn.transaction():
                await promote_storage_scope_epochs(
                    conn,
                    measurement_basis=measurement_basis,
                    activated_at=activated_at,
                    source_cluster=source_cluster,
                    namespaces=namespaces,
                    max_scope_age=max_scope_age,
                    expected_generation=expected_generation,
                    identity_key_version=identity_key_version,
                )
                return await schedule_storage_activation(
                    conn,
                    measurement_basis=measurement_basis,
                    activated_at=activated_at,
                )

    async def assert_destroyed(
        self,
        *,
        idempotency_key: UUID,
        asset_id: UUID,
        effective_at: datetime,
        evidence_kind: str,
        evidence_digest: str,
        actor_kind: str,
        actor_id: UUID | None,
        reason_code: str,
    ) -> BackendDestructionResult:
        async with self._app.acquire() as conn:
            async with conn.transaction():
                return await assert_backend_destroyed(
                    conn,
                    idempotency_key=idempotency_key,
                    asset_id=asset_id,
                    effective_at=effective_at,
                    evidence_kind=evidence_kind,
                    evidence_digest=evidence_digest,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    reason_code=reason_code,
                )

    async def list_backend_unverified(
        self,
        *,
        limit: int = 100,
        after_asset_id: UUID | None = None,
    ) -> BackendUnverifiedAssetPage:
        async with self._app.acquire() as conn:
            async with conn.transaction(readonly=True):
                return await list_backend_unverified_assets(
                    conn,
                    limit=limit,
                    after_asset_id=after_asset_id,
                )

    async def read_asset_detail(
        self,
        *,
        asset_id: UUID,
        history_limit: int = 100,
    ) -> StorageAssetDetailRecord:
        async with self._app.acquire() as conn:
            async with conn.transaction(readonly=True):
                return await read_storage_asset_detail(
                    conn,
                    asset_id=asset_id,
                    history_limit=history_limit,
                )


__all__ = [
    "BackendDestructionResult",
    "BackendGapRecord",
    "BackendUnverifiedAssetPage",
    "BackendUnverifiedAssetRecord",
    "DerivedVolumeAssetIdentity",
    "StorageActivation",
    "StorageActivationNotReady",
    "StorageAssetConflict",
    "StorageAssetContractError",
    "StorageAssetError",
    "StorageAssetAssertionDetail",
    "StorageAssetDetailRecord",
    "StorageAssetGapDetail",
    "StorageAssetIncarnationDetail",
    "StorageAssetNotFound",
    "StorageAssetStore",
    "VolumeAssetRecord",
    "VolumeIncarnationRecord",
    "advance_storage_activation_to_shadow",
    "assert_backend_destroyed",
    "backend_destruction_request_hash",
    "derive_volume_asset_identity",
    "ensure_volume_asset",
    "lock_storage_activation",
    "list_backend_unverified_assets",
    "observe_volume_incarnation",
    "open_backend_unverified_gap",
    "promote_storage_scope_epochs",
    "read_storage_activation",
    "read_storage_asset_detail",
    "register_storage_identity_key",
    "resolve_backend_gap_reobserved",
    "schedule_storage_activation",
    "volume_identity_key_fingerprint",
]
