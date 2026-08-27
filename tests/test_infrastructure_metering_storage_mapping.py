"""Trusted server-side storage resource mapping contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from orchestrator.services.infrastructure_metering.storage_mapping import (
    StorageResourceMappingConflict,
    StorageResourceMappingContractError,
    StorageResourceMappingKey,
    StorageResourceMappingRule,
    StorageResourceMappingStore,
    UNMAPPED_BLOCK_VOLUME_RESOURCE,
    register_storage_resource_mapping,
    resolve_storage_resource_mapping,
    validate_storage_resource_mapping_set,
)


_REGISTERED_AT = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)


def _rule(**overrides: Any) -> StorageResourceMappingRule:
    values = {
        "source_cluster": "main-dev",
        "storage_class_name": "premium-rwo",
        "csi_driver": "csi.example.test",
        "volume_mode": "filesystem",
        "resource": "block_volume_stackit_premium",
        "mapping_version": "stackit-2026-08-v1",
    }
    values.update(overrides)
    return StorageResourceMappingRule(**values)


def _row(rule: StorageResourceMappingRule) -> dict[str, Any]:
    return {
        "source_cluster": rule.source_cluster,
        "storage_class_name": rule.storage_class_name,
        "csi_driver": rule.csi_driver,
        "volume_mode": rule.volume_mode,
        "resource": rule.resource,
        "mapping_version": rule.mapping_version,
        "rule_fingerprint": rule.fingerprint,
        "registered_at": _REGISTERED_AT,
    }


class _MemoryConnection:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str | None, str | None, str], dict[str, Any]] = {}
        self.transactions: list[dict[str, Any]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "storage-resource-mapping:register" in query:
            rule = StorageResourceMappingRule(
                source_cluster=args[0],
                storage_class_name=args[1],
                csi_driver=args[2],
                volume_mode=args[3],
                resource=args[4],
                mapping_version=args[5],
            )
            key = (
                rule.source_cluster,
                rule.storage_class_name,
                rule.csi_driver,
                rule.volume_mode,
            )
            if key in self.rows:
                return None
            value = _row(rule)
            assert value["rule_fingerprint"] == args[6]
            self.rows[key] = value
            return value
        if (
            "storage-resource-mapping:select-exact" in query
            or "storage-resource-mapping:resolve-exact" in query
        ):
            return self.rows.get((args[0], args[1], args[2], args[3]))
        raise AssertionError(query)

    async def fetch(self, query: str, *_args: Any) -> list[dict[str, Any]]:
        if "storage-resource-mapping:list-resources" in query:
            return [
                {"resource": resource}
                for resource in sorted({row["resource"] for row in self.rows.values()})
            ]
        raise AssertionError(query)

    def transaction(self, **kwargs: Any) -> _Transaction:
        self.transactions.append(kwargs)
        return _Transaction()


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> None:
        return None


class _Acquire:
    def __init__(self, conn: _MemoryConnection) -> None:
        self.conn = conn

    async def __aenter__(self) -> _MemoryConnection:
        return self.conn

    async def __aexit__(self, *args: Any) -> None:
        return None


class _Pool:
    def __init__(self, conn: _MemoryConnection) -> None:
        self.conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


def test_mapping_fingerprint_is_deterministic_and_covers_nullable_exact_key() -> None:
    rule = _rule()
    assert rule.fingerprint == _rule().fingerprint
    assert len(rule.fingerprint) == 64
    assert rule.fingerprint != _rule(storage_class_name=None).fingerprint
    assert rule.fingerprint != _rule(csi_driver=None).fingerprint
    assert rule.fingerprint != _rule(volume_mode="block").fingerprint
    assert (
        rule.fingerprint != _rule(resource="block_volume_stackit_standard").fingerprint
    )
    assert rule.fingerprint != _rule(mapping_version="stackit-2026-08-v2").fingerprint


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_cluster", "unknown"),
        ("source_cluster", "cluster-*"),
        ("storage_class_name", "*"),
        ("storage_class_name", "UNKNOWN"),
        ("csi_driver", "driver?.example.test"),
        ("csi_driver", "unknown"),
        ("volume_mode", "unknown"),
        ("volume_mode", "Filesystem"),
        ("resource", "unmapped_block_volume"),
        ("resource", "block-volume-premium"),
        ("resource", "block_volume_UPPER"),
        ("mapping_version", "*"),
        ("mapping_version", "v" * 65),
    ),
)
def test_mapping_rule_rejects_unknown_wildcard_and_untyped_values(
    field: str, value: str
) -> None:
    with pytest.raises(StorageResourceMappingContractError):
        _rule(**{field: value})


def test_mapping_set_rejects_duplicate_exact_keys_even_when_output_matches() -> None:
    first = _rule()
    with pytest.raises(
        StorageResourceMappingContractError, match="duplicate exact key"
    ):
        validate_storage_resource_mapping_set((first, _rule()))
    with pytest.raises(
        StorageResourceMappingContractError, match="duplicate exact key"
    ):
        validate_storage_resource_mapping_set(
            (first, _rule(resource="block_volume_stackit_standard"))
        )


@pytest.mark.asyncio
async def test_registration_is_append_only_and_exact_replay_is_verified() -> None:
    conn = _MemoryConnection()
    rule = _rule()

    created = await register_storage_resource_mapping(conn, rule)
    replay = await register_storage_resource_mapping(conn, rule)

    assert not created.replayed
    assert replay.replayed
    assert created.fingerprint == replay.fingerprint == rule.fingerprint
    assert created.registered_at == replay.registered_at == _REGISTERED_AT

    with pytest.raises(
        StorageResourceMappingConflict, match="different immutable intent"
    ):
        await register_storage_resource_mapping(
            conn,
            _rule(resource="block_volume_stackit_standard"),
        )


@pytest.mark.asyncio
async def test_resolver_never_falls_back_across_nullable_or_other_exact_keys() -> None:
    conn = _MemoryConnection()
    nullable = _rule(storage_class_name=None, csi_driver=None)
    await register_storage_resource_mapping(conn, nullable)

    found = await resolve_storage_resource_mapping(conn, nullable.key)
    assert found.mapped
    assert found.resource == nullable.resource
    assert found.mapping_version == nullable.mapping_version
    assert found.rule_fingerprint == nullable.fingerprint

    missing = await resolve_storage_resource_mapping(
        conn,
        StorageResourceMappingKey(
            source_cluster=nullable.source_cluster,
            storage_class_name="some-class",
            csi_driver=None,
            volume_mode=nullable.volume_mode,
        ),
    )
    assert not missing.mapped
    assert missing.resource == UNMAPPED_BLOCK_VOLUME_RESOURCE
    assert missing.mapping_version is None
    assert missing.rule_fingerprint is None


@pytest.mark.asyncio
async def test_resolver_fails_closed_on_persisted_fingerprint_drift() -> None:
    conn = _MemoryConnection()
    rule = _rule()
    await register_storage_resource_mapping(conn, rule)
    conn.rows[
        (
            rule.source_cluster,
            rule.storage_class_name,
            rule.csi_driver,
            rule.volume_mode,
        )
    ]["rule_fingerprint"] = "0" * 64

    with pytest.raises(StorageResourceMappingConflict, match="fingerprint is invalid"):
        await resolve_storage_resource_mapping(conn, rule.key)


@pytest.mark.asyncio
async def test_store_resolver_owns_a_read_only_transaction() -> None:
    conn = _MemoryConnection()
    store = StorageResourceMappingStore(_Pool(conn))  # type: ignore[arg-type]
    rule = _rule()

    registrations = await store.register((rule,))
    resolution = await store.resolve(rule.key)
    resources = await store.resources()

    assert len(registrations) == 1
    assert resolution.resource == rule.resource
    assert resources == (rule.resource,)
    assert conn.transactions == [{}, {"readonly": True}, {"readonly": True}]
