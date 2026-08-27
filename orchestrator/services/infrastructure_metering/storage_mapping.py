"""Trusted, immutable mapping of observed volumes to priced resource names.

Collector payloads deliberately remain untrusted and emit
``unmapped_block_volume``.  Only the orchestrator may apply these exact-match
rules after ingestion.  ``None`` in either nullable key column is an ordinary
SQL value (matched with ``IS NOT DISTINCT FROM``), never a wildcard.

The module expects ``infrastructure_storage_resource_mappings`` to have these
columns and no mutable-rule semantics::

    source_cluster       VARCHAR(255) NOT NULL
    storage_class_name   VARCHAR(253) NULL
    csi_driver           VARCHAR(253) NULL
    volume_mode          VARCHAR(10) NOT NULL
    resource             VARCHAR(128) NOT NULL
    mapping_version      VARCHAR(64) NOT NULL
    rule_fingerprint     CHAR(64) NOT NULL
    registered_at        TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp()

The four key columns require one ``UNIQUE NULLS NOT DISTINCT`` constraint and
``rule_fingerprint`` requires a separate unique constraint.  Rows are
append-only; an attempted remap of an existing key fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

import asyncpg


UNMAPPED_BLOCK_VOLUME_RESOURCE = "unmapped_block_volume"

_FINGERPRINT_CONTEXT = b"srw-infrastructure-storage-resource-mapping-v1\x00"
_CLUSTER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_DNS_LABEL = r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
_KUBERNETES_NAME_RE = re.compile(rf"^{_DNS_LABEL}(?:\.{_DNS_LABEL})*$")
_RESOURCE_RE = re.compile(r"^block_volume_[a-z0-9_]+$")
_MAPPING_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_WILDCARD_CHARACTERS = frozenset("*?[]{}")
_UNKNOWN_SENTINELS = frozenset({"unknown", "unmapped", "any"})
_VOLUME_MODES = frozenset({"filesystem", "block"})

_INSERT_SQL = """
/* storage-resource-mapping:register */
INSERT INTO infrastructure_storage_resource_mappings (
    source_cluster, storage_class_name, csi_driver, volume_mode,
    resource, mapping_version, rule_fingerprint
) VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT DO NOTHING
RETURNING source_cluster, storage_class_name, csi_driver, volume_mode,
          resource, mapping_version, rule_fingerprint, registered_at
"""

_SELECT_EXACT_SQL = """
/* storage-resource-mapping:select-exact */
SELECT source_cluster, storage_class_name, csi_driver, volume_mode,
       resource, mapping_version, rule_fingerprint, registered_at
FROM infrastructure_storage_resource_mappings
WHERE source_cluster = $1
  AND storage_class_name IS NOT DISTINCT FROM $2
  AND csi_driver IS NOT DISTINCT FROM $3
  AND volume_mode = $4
FOR SHARE
"""

_RESOLVE_EXACT_SQL = """
/* storage-resource-mapping:resolve-exact */
SELECT source_cluster, storage_class_name, csi_driver, volume_mode,
       resource, mapping_version, rule_fingerprint, registered_at
FROM infrastructure_storage_resource_mappings
WHERE source_cluster = $1
  AND storage_class_name IS NOT DISTINCT FROM $2
  AND csi_driver IS NOT DISTINCT FROM $3
  AND volume_mode = $4
"""

_LIST_RESOURCES_SQL = """
/* storage-resource-mapping:list-resources */
SELECT DISTINCT resource
FROM infrastructure_storage_resource_mappings
ORDER BY resource
"""


class StorageResourceMappingError(RuntimeError):
    """Base class for trusted storage-mapping failures."""


class StorageResourceMappingContractError(StorageResourceMappingError, ValueError):
    """A mapping rule or lookup key is outside the strict contract."""


class StorageResourceMappingConflict(StorageResourceMappingError):
    """An immutable key already maps differently or stored data is invalid."""


def _contains_wildcard(value: str) -> bool:
    return any(character in value for character in _WILDCARD_CHARACTERS)


def _exact_text(
    value: Any,
    name: str,
    *,
    maximum: int,
    pattern: re.Pattern[str],
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or pattern.fullmatch(value) is None
        or _contains_wildcard(value)
        or value.casefold() in _UNKNOWN_SENTINELS
    ):
        raise StorageResourceMappingContractError(f"{name} is not an exact value")
    return value


def _optional_kubernetes_name(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _exact_text(
        value,
        name,
        maximum=253,
        pattern=_KUBERNETES_NAME_RE,
    )


@dataclass(frozen=True, slots=True)
class StorageResourceMappingKey:
    """One exact lookup key; nullable components never mean fallback."""

    source_cluster: str
    storage_class_name: str | None
    csi_driver: str | None
    volume_mode: str

    def __post_init__(self) -> None:
        _exact_text(
            self.source_cluster,
            "source_cluster",
            maximum=255,
            pattern=_CLUSTER_RE,
        )
        _optional_kubernetes_name(self.storage_class_name, "storage_class_name")
        _optional_kubernetes_name(self.csi_driver, "csi_driver")
        if (
            not isinstance(self.volume_mode, str)
            or self.volume_mode not in _VOLUME_MODES
        ):
            raise StorageResourceMappingContractError(
                "volume_mode must be filesystem or block"
            )


@dataclass(frozen=True, slots=True)
class StorageResourceMappingRule:
    """One validated, immutable, server-owned mapping rule."""

    source_cluster: str
    storage_class_name: str | None
    csi_driver: str | None
    volume_mode: str
    resource: str
    mapping_version: str

    def __post_init__(self) -> None:
        StorageResourceMappingKey(
            source_cluster=self.source_cluster,
            storage_class_name=self.storage_class_name,
            csi_driver=self.csi_driver,
            volume_mode=self.volume_mode,
        )
        if (
            not isinstance(self.resource, str)
            or len(self.resource) > 128
            or _RESOURCE_RE.fullmatch(self.resource) is None
        ):
            raise StorageResourceMappingContractError(
                "resource must match ^block_volume_[a-z0-9_]+$"
            )
        _exact_text(
            self.mapping_version,
            "mapping_version",
            maximum=64,
            pattern=_MAPPING_VERSION_RE,
        )

    @property
    def key(self) -> StorageResourceMappingKey:
        return StorageResourceMappingKey(
            source_cluster=self.source_cluster,
            storage_class_name=self.storage_class_name,
            csi_driver=self.csi_driver,
            volume_mode=self.volume_mode,
        )

    @property
    def fingerprint(self) -> str:
        return storage_resource_mapping_fingerprint(self)


@dataclass(frozen=True, slots=True)
class StorageResourceMappingRegistration:
    rule: StorageResourceMappingRule
    fingerprint: str
    registered_at: datetime
    replayed: bool


@dataclass(frozen=True, slots=True)
class StorageResourceMappingResolution:
    key: StorageResourceMappingKey
    resource: str
    mapping_version: str | None
    rule_fingerprint: str | None

    @property
    def mapped(self) -> bool:
        return self.rule_fingerprint is not None


def storage_resource_mapping_fingerprint(rule: StorageResourceMappingRule) -> str:
    """Return a stable digest over every exact input and output field."""

    if not isinstance(rule, StorageResourceMappingRule):
        raise StorageResourceMappingContractError(
            "rule must be a StorageResourceMappingRule"
        )
    payload = json.dumps(
        {
            "csi_driver": rule.csi_driver,
            "mapping_version": rule.mapping_version,
            "resource": rule.resource,
            "source_cluster": rule.source_cluster,
            "storage_class_name": rule.storage_class_name,
            "volume_mode": rule.volume_mode,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(_FINGERPRINT_CONTEXT + payload).hexdigest()


def validate_storage_resource_mapping_set(
    rules: Iterable[StorageResourceMappingRule],
) -> tuple[StorageResourceMappingRule, ...]:
    """Materialize a rule set and reject duplicate exact keys before SQL."""

    try:
        materialized = tuple(rules)
    except TypeError as exc:
        raise StorageResourceMappingContractError("rules must be iterable") from exc
    seen: set[StorageResourceMappingKey] = set()
    fingerprints: set[str] = set()
    for rule in materialized:
        if not isinstance(rule, StorageResourceMappingRule):
            raise StorageResourceMappingContractError(
                "every rule must be a StorageResourceMappingRule"
            )
        if rule.key in seen:
            raise StorageResourceMappingContractError(
                "mapping rule set contains a duplicate exact key"
            )
        seen.add(rule.key)
        if rule.fingerprint in fingerprints:
            raise StorageResourceMappingContractError(
                "mapping rule set contains a duplicate fingerprint"
            )
        fingerprints.add(rule.fingerprint)
    return materialized


def _stored_registration(
    row: Mapping[str, Any],
    *,
    intended: StorageResourceMappingRule,
    replayed: bool,
) -> StorageResourceMappingRegistration:
    try:
        stored = StorageResourceMappingRule(
            source_cluster=str(row["source_cluster"]),
            storage_class_name=(
                None
                if row["storage_class_name"] is None
                else str(row["storage_class_name"])
            ),
            csi_driver=(None if row["csi_driver"] is None else str(row["csi_driver"])),
            volume_mode=str(row["volume_mode"]),
            resource=str(row["resource"]),
            mapping_version=str(row["mapping_version"]),
        )
        fingerprint = str(row["rule_fingerprint"])
        registered_at = row["registered_at"]
        if (
            not isinstance(registered_at, datetime)
            or registered_at.tzinfo is None
            or registered_at.utcoffset() is None
            or _FINGERPRINT_RE.fullmatch(fingerprint) is None
        ):
            raise ValueError("invalid registration metadata")
    except (
        KeyError,
        TypeError,
        ValueError,
        StorageResourceMappingContractError,
    ) as exc:
        raise StorageResourceMappingConflict(
            "persisted storage resource mapping violates its contract"
        ) from exc
    if stored != intended or fingerprint != intended.fingerprint:
        raise StorageResourceMappingConflict(
            "storage resource mapping key already has different immutable intent"
        )
    return StorageResourceMappingRegistration(
        rule=stored,
        fingerprint=fingerprint,
        registered_at=registered_at,
        replayed=replayed,
    )


async def register_storage_resource_mapping(
    conn: asyncpg.Connection,
    rule: StorageResourceMappingRule,
) -> StorageResourceMappingRegistration:
    """Insert or exactly replay one mapping; never update an existing key."""

    if not isinstance(rule, StorageResourceMappingRule):
        raise StorageResourceMappingContractError(
            "rule must be a StorageResourceMappingRule"
        )
    args = (
        rule.source_cluster,
        rule.storage_class_name,
        rule.csi_driver,
        rule.volume_mode,
        rule.resource,
        rule.mapping_version,
        rule.fingerprint,
    )
    inserted = await conn.fetchrow(_INSERT_SQL, *args)
    row = inserted
    replayed = False
    if row is None:
        row = await conn.fetchrow(
            _SELECT_EXACT_SQL,
            rule.source_cluster,
            rule.storage_class_name,
            rule.csi_driver,
            rule.volume_mode,
        )
        replayed = True
    if row is None:
        raise StorageResourceMappingConflict(
            "storage resource mapping registration collided outside its exact key"
        )
    return _stored_registration(row, intended=rule, replayed=replayed)


async def register_storage_resource_mappings(
    conn: asyncpg.Connection,
    rules: Iterable[StorageResourceMappingRule],
) -> tuple[StorageResourceMappingRegistration, ...]:
    """Register one duplicate-free set inside the caller's transaction."""

    validated = validate_storage_resource_mapping_set(rules)
    registrations: list[StorageResourceMappingRegistration] = []
    for rule in validated:
        registrations.append(await register_storage_resource_mapping(conn, rule))
    return tuple(registrations)


async def resolve_storage_resource_mapping(
    conn: asyncpg.Connection,
    key: StorageResourceMappingKey,
) -> StorageResourceMappingResolution:
    """Resolve only the exact key, returning an explicitly unpriced fallback."""

    if not isinstance(key, StorageResourceMappingKey):
        raise StorageResourceMappingContractError(
            "key must be a StorageResourceMappingKey"
        )
    row = await conn.fetchrow(
        _RESOLVE_EXACT_SQL,
        key.source_cluster,
        key.storage_class_name,
        key.csi_driver,
        key.volume_mode,
    )
    if row is None:
        return StorageResourceMappingResolution(
            key=key,
            resource=UNMAPPED_BLOCK_VOLUME_RESOURCE,
            mapping_version=None,
            rule_fingerprint=None,
        )
    try:
        rule = StorageResourceMappingRule(
            source_cluster=str(row["source_cluster"]),
            storage_class_name=(
                None
                if row["storage_class_name"] is None
                else str(row["storage_class_name"])
            ),
            csi_driver=(None if row["csi_driver"] is None else str(row["csi_driver"])),
            volume_mode=str(row["volume_mode"]),
            resource=str(row["resource"]),
            mapping_version=str(row["mapping_version"]),
        )
        fingerprint = str(row["rule_fingerprint"])
    except (KeyError, TypeError, StorageResourceMappingContractError) as exc:
        raise StorageResourceMappingConflict(
            "persisted storage resource mapping violates its contract"
        ) from exc
    if rule.key != key or fingerprint != rule.fingerprint:
        raise StorageResourceMappingConflict(
            "persisted storage resource mapping fingerprint is invalid"
        )
    return StorageResourceMappingResolution(
        key=key,
        resource=rule.resource,
        mapping_version=rule.mapping_version,
        rule_fingerprint=fingerprint,
    )


async def list_storage_resource_mapping_resources(
    conn: asyncpg.Connection,
) -> tuple[str, ...]:
    rows = await conn.fetch(_LIST_RESOURCES_SQL)
    resources = tuple(str(row["resource"]) for row in rows)
    if len(set(resources)) != len(resources) or any(
        len(resource) > 128 or _RESOURCE_RE.fullmatch(resource) is None
        for resource in resources
    ):
        raise StorageResourceMappingConflict(
            "persisted storage resource mapping output is invalid"
        )
    return resources


class StorageResourceMappingStore:
    """Transaction boundary for trusted registrations and exact lookups."""

    def __init__(self, app_pool: asyncpg.Pool) -> None:
        self._app = app_pool

    async def register(
        self, rules: Iterable[StorageResourceMappingRule]
    ) -> tuple[StorageResourceMappingRegistration, ...]:
        validated = validate_storage_resource_mapping_set(rules)
        async with self._app.acquire() as conn:
            async with conn.transaction():
                return await register_storage_resource_mappings(conn, validated)

    async def resolve(
        self, key: StorageResourceMappingKey
    ) -> StorageResourceMappingResolution:
        async with self._app.acquire() as conn:
            async with conn.transaction(readonly=True):
                return await resolve_storage_resource_mapping(conn, key)

    async def resources(self) -> tuple[str, ...]:
        async with self._app.acquire() as conn:
            async with conn.transaction(readonly=True):
                return await list_storage_resource_mapping_resources(conn)


__all__ = [
    "StorageResourceMappingConflict",
    "StorageResourceMappingContractError",
    "StorageResourceMappingError",
    "StorageResourceMappingKey",
    "StorageResourceMappingRegistration",
    "StorageResourceMappingResolution",
    "StorageResourceMappingRule",
    "StorageResourceMappingStore",
    "UNMAPPED_BLOCK_VOLUME_RESOURCE",
    "register_storage_resource_mapping",
    "register_storage_resource_mappings",
    "resolve_storage_resource_mapping",
    "list_storage_resource_mapping_resources",
    "storage_resource_mapping_fingerprint",
    "validate_storage_resource_mapping_set",
]
