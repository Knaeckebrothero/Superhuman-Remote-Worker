"""Fail-closed rollout settings for infrastructure metering."""

from __future__ import annotations

import os
import re
import json
from dataclasses import dataclass
from typing import Literal, Mapping

from orchestrator.services.infrastructure_metering.storage_mapping import (
    StorageResourceMappingContractError,
    StorageResourceMappingRule,
    validate_storage_resource_mapping_set,
)

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})
_STABLE_CLUSTER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VOLUME_IDENTITY_KEY_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_KUBERNETES_NAMESPACE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


def _flag(source: Mapping[str, str], name: str, *, default: bool = False) -> bool:
    raw = source.get(name, "true" if default else "false").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _bounded_int(
    source: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = source.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _namespace_allowlist(source: Mapping[str, str]) -> tuple[str, ...]:
    raw = source.get("INFRASTRUCTURE_METERING_NAMESPACE_ALLOWLIST", "")
    namespaces = tuple(
        dict.fromkeys(part.strip() for part in raw.split(",") if part.strip())
    )
    invalid = [name for name in namespaces if not _KUBERNETES_NAMESPACE.fullmatch(name)]
    if invalid:
        raise ValueError(
            "INFRASTRUCTURE_METERING_NAMESPACE_ALLOWLIST contains invalid "
            f"Kubernetes namespaces: {', '.join(invalid)}"
        )
    return namespaces


def _volume_resource_mappings(
    source: Mapping[str, str],
    *,
    source_cluster: str,
) -> tuple[StorageResourceMappingRule, ...]:
    name = "INFRASTRUCTURE_METERING_VOLUME_RESOURCE_MAPPINGS_JSON"
    raw = source.get(name, "[]").strip() or "[]"
    if len(raw.encode("utf-8")) > 65_536:
        raise ValueError(f"{name} must be at most 65536 UTF-8 bytes")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON") from exc
    if not isinstance(decoded, list) or len(decoded) > 100:
        raise ValueError(f"{name} must be an array with at most 100 rules")
    if decoded and not source_cluster:
        raise ValueError(f"{name} requires a stable cluster id")
    expected = {
        "mappingVersion",
        "storageClass",
        "csiDriver",
        "volumeMode",
        "resource",
    }
    rules: list[StorageResourceMappingRule] = []
    for index, item in enumerate(decoded):
        if not isinstance(item, dict) or set(item) != expected:
            raise ValueError(
                f"{name}[{index}] must contain exactly: " + ", ".join(sorted(expected))
            )
        storage_class = item["storageClass"]
        csi_driver = item["csiDriver"]
        if storage_class == "":
            storage_class = None
        if csi_driver == "":
            csi_driver = None
        try:
            rules.append(
                StorageResourceMappingRule(
                    source_cluster=source_cluster,
                    storage_class_name=storage_class,
                    csi_driver=csi_driver,
                    volume_mode=item["volumeMode"],
                    resource=item["resource"],
                    mapping_version=item["mappingVersion"],
                )
            )
        except (StorageResourceMappingContractError, TypeError) as exc:
            raise ValueError(f"{name}[{index}] is invalid: {exc}") from exc
    try:
        return validate_storage_resource_mapping_set(rules)
    except StorageResourceMappingContractError as exc:
        raise ValueError(f"{name} is invalid: {exc}") from exc


@dataclass(frozen=True)
class InfrastructureMeteringSettings:
    """Independent dark-launch gates; every gate defaults to off.

    ``publication_enabled`` is intentionally the strictest gate. Merely
    enabling v2 reads or shadow collection can never cause ledger writes.
    Runtime schema/source capability checks provide a second, independent
    publication fence around the Slice 1 publisher.
    """

    v2_reads_enabled: bool = False
    source_aware_reads_enabled: bool = False
    collector_enabled: bool = False
    shadow_enabled: bool = False
    cutover_enabled: bool = False
    publication_enabled: bool = False
    pvc_inventory_enabled: bool = False
    pv_inventory_enabled: bool = False
    pvc_shadow_enabled: bool = False
    pv_shadow_enabled: bool = False
    pvc_publication_enabled: bool = False
    pv_publication_enabled: bool = False
    ide_pod_shadow_enabled: bool = False
    agent_pod_shadow_enabled: bool = False
    ide_pod_publication_enabled: bool = False
    agent_pod_publication_enabled: bool = False
    vm_inventory_enabled: bool = False
    vm_shadow_enabled: bool = False
    vm_publication_enabled: bool = False
    vm_pvc_inventory_enabled: bool = False
    vm_pv_inventory_enabled: bool = False
    vm_pvc_shadow_enabled: bool = False
    vm_pv_shadow_enabled: bool = False
    vm_pvc_publication_enabled: bool = False
    vm_pv_publication_enabled: bool = False
    vm_pv_cluster_wide_rbac_acknowledged: bool = False
    vm_stable_cluster_id: str = ""
    vm_namespace: str = ""
    volume_identity_key_version: str = ""
    volume_resource_mappings: tuple[StorageResourceMappingRule, ...] = ()
    stable_cluster_id: str = ""
    deployment_mode: Literal["dedicated", "in-process"] = "dedicated"
    namespace_allowlist: tuple[str, ...] = ()
    relist_interval_seconds: int = 300
    stale_after_seconds: int = 900
    max_collector_clock_skew_seconds: int = 300
    list_page_size: int = 500
    scope_concurrency: int = 2
    watch_queue_size: int = 10_000
    max_snapshot_items: int = 50_000
    max_snapshot_bytes: int = 64 * 1024 * 1024
    ingestion_ticket_ttl_seconds: int = 600
    snapshot_item_retention_days: int = 7
    diagnostic_retention_days: int = 35
    cleanup_interval_seconds: int = 300

    @classmethod
    def from_env(
        cls, source: Mapping[str, str] | None = None
    ) -> "InfrastructureMeteringSettings":
        env = os.environ if source is None else source
        deployment_mode = env.get(
            "INFRASTRUCTURE_METERING_DEPLOYMENT_MODE", "dedicated"
        ).strip()
        if deployment_mode not in {"dedicated", "in-process"}:
            raise ValueError(
                "INFRASTRUCTURE_METERING_DEPLOYMENT_MODE must be "
                "'dedicated' or 'in-process'"
            )
        stable_cluster_id = env.get(
            "INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID", ""
        ).strip()
        settings = cls(
            v2_reads_enabled=_flag(env, "INFRASTRUCTURE_METERING_V2_READS_ENABLED"),
            source_aware_reads_enabled=_flag(
                env,
                "INFRASTRUCTURE_METERING_SOURCE_AWARE_READS_ENABLED",
            ),
            collector_enabled=_flag(env, "INFRASTRUCTURE_METERING_COLLECTOR_ENABLED"),
            shadow_enabled=_flag(env, "INFRASTRUCTURE_METERING_SHADOW_ENABLED"),
            cutover_enabled=_flag(
                env,
                "INFRASTRUCTURE_METERING_CUTOVER_ENABLED",
            ),
            publication_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_PUBLICATION_ENABLED"
            ),
            pvc_inventory_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_PVC_INVENTORY_ENABLED"
            ),
            pv_inventory_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_PV_INVENTORY_ENABLED"
            ),
            pvc_shadow_enabled=_flag(env, "INFRASTRUCTURE_METERING_PVC_SHADOW_ENABLED"),
            pv_shadow_enabled=_flag(env, "INFRASTRUCTURE_METERING_PV_SHADOW_ENABLED"),
            pvc_publication_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_PVC_PUBLICATION_ENABLED"
            ),
            pv_publication_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_PV_PUBLICATION_ENABLED"
            ),
            ide_pod_shadow_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_IDE_POD_SHADOW_ENABLED"
            ),
            agent_pod_shadow_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_AGENT_POD_SHADOW_ENABLED"
            ),
            ide_pod_publication_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_IDE_POD_PUBLICATION_ENABLED"
            ),
            agent_pod_publication_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_AGENT_POD_PUBLICATION_ENABLED"
            ),
            vm_inventory_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_VM_INVENTORY_ENABLED"
            ),
            vm_shadow_enabled=_flag(env, "INFRASTRUCTURE_METERING_VM_SHADOW_ENABLED"),
            vm_publication_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_VM_PUBLICATION_ENABLED"
            ),
            vm_pvc_inventory_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_VM_PVC_INVENTORY_ENABLED"
            ),
            vm_pv_inventory_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_VM_PV_INVENTORY_ENABLED"
            ),
            vm_pvc_shadow_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_VM_PVC_SHADOW_ENABLED"
            ),
            vm_pv_shadow_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_VM_PV_SHADOW_ENABLED"
            ),
            vm_pvc_publication_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_VM_PVC_PUBLICATION_ENABLED"
            ),
            vm_pv_publication_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_VM_PV_PUBLICATION_ENABLED"
            ),
            vm_pv_cluster_wide_rbac_acknowledged=_flag(
                env,
                "INFRASTRUCTURE_METERING_VM_PV_CLUSTER_WIDE_RBAC_ACKNOWLEDGED",
            ),
            vm_stable_cluster_id=env.get(
                "INFRASTRUCTURE_METERING_VM_STABLE_CLUSTER_ID", ""
            ).strip(),
            vm_namespace=env.get("INFRASTRUCTURE_METERING_VM_NAMESPACE", "").strip(),
            volume_identity_key_version=env.get(
                "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY_VERSION", ""
            ).strip(),
            volume_resource_mappings=_volume_resource_mappings(
                env,
                source_cluster=stable_cluster_id,
            ),
            stable_cluster_id=stable_cluster_id,
            deployment_mode=deployment_mode,
            namespace_allowlist=_namespace_allowlist(env),
            relist_interval_seconds=_bounded_int(
                env,
                "INFRASTRUCTURE_METERING_RELIST_INTERVAL_SECONDS",
                300,
                minimum=15,
                maximum=86_400,
            ),
            stale_after_seconds=_bounded_int(
                env,
                "INFRASTRUCTURE_METERING_STALE_AFTER_SECONDS",
                900,
                minimum=30,
                maximum=604_800,
            ),
            max_collector_clock_skew_seconds=_bounded_int(
                env,
                "INFRASTRUCTURE_METERING_MAX_COLLECTOR_CLOCK_SKEW_SECONDS",
                300,
                minimum=1,
                maximum=3_600,
            ),
            list_page_size=_bounded_int(
                env,
                "INFRASTRUCTURE_METERING_LIST_PAGE_SIZE",
                500,
                minimum=1,
                maximum=5_000,
            ),
            scope_concurrency=_bounded_int(
                env,
                "INFRASTRUCTURE_METERING_SCOPE_CONCURRENCY",
                2,
                minimum=1,
                maximum=32,
            ),
            watch_queue_size=_bounded_int(
                env,
                "INFRASTRUCTURE_METERING_WATCH_QUEUE_SIZE",
                10_000,
                minimum=100,
                maximum=1_000_000,
            ),
            max_snapshot_items=_bounded_int(
                env,
                "INFRASTRUCTURE_METERING_MAX_SNAPSHOT_ITEMS",
                50_000,
                minimum=1,
                maximum=1_000_000,
            ),
            max_snapshot_bytes=_bounded_int(
                env,
                "INFRASTRUCTURE_METERING_MAX_SNAPSHOT_BYTES",
                64 * 1024 * 1024,
                minimum=1_048_576,
                maximum=1_073_741_824,
            ),
            ingestion_ticket_ttl_seconds=_bounded_int(
                env,
                "INFRASTRUCTURE_METERING_INGESTION_TICKET_TTL_SECONDS",
                600,
                minimum=10,
                maximum=600,
            ),
            snapshot_item_retention_days=_bounded_int(
                env,
                "INFRASTRUCTURE_METERING_SNAPSHOT_ITEM_RETENTION_DAYS",
                7,
                minimum=7,
                maximum=365,
            ),
            diagnostic_retention_days=_bounded_int(
                env,
                "INFRASTRUCTURE_METERING_DIAGNOSTIC_RETENTION_DAYS",
                35,
                minimum=7,
                maximum=3_650,
            ),
            cleanup_interval_seconds=_bounded_int(
                env,
                "INFRASTRUCTURE_METERING_CLEANUP_INTERVAL_SECONDS",
                300,
                minimum=30,
                maximum=3_600,
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.stable_cluster_id and not _STABLE_CLUSTER_ID.fullmatch(
            self.stable_cluster_id
        ):
            raise ValueError(
                "infrastructure metering stable cluster id must be 1-128 "
                "characters using letters, digits, '.', '_', ':', or '-'"
            )
        if self.vm_stable_cluster_id and not _STABLE_CLUSTER_ID.fullmatch(
            self.vm_stable_cluster_id
        ):
            raise ValueError(
                "infrastructure metering VM stable cluster id must be 1-128 "
                "characters using letters, digits, '.', '_', ':', or '-'"
            )
        if self.vm_namespace and not _KUBERNETES_NAMESPACE.fullmatch(self.vm_namespace):
            raise ValueError(
                "infrastructure metering VM namespace must be a valid "
                "Kubernetes namespace"
            )
        if self.volume_identity_key_version and not (
            _VOLUME_IDENTITY_KEY_VERSION.fullmatch(self.volume_identity_key_version)
        ):
            raise ValueError(
                "infrastructure metering volume identity key version must be "
                "1-64 characters using letters, digits, '.', '_', ':', or '-'"
            )
        validate_storage_resource_mapping_set(self.volume_resource_mappings)
        if any(
            rule.source_cluster != self.stable_cluster_id
            for rule in self.volume_resource_mappings
        ):
            raise ValueError(
                "infrastructure storage resource mappings must use the stable "
                "cluster id"
            )
        if self.shadow_enabled and not self.collector_enabled:
            raise ValueError(
                "infrastructure metering shadow mode requires its collector"
            )
        if self.pvc_inventory_enabled and not self.collector_enabled:
            raise ValueError(
                "infrastructure metering PVC inventory requires its collector"
            )
        if self.pv_inventory_enabled and not self.collector_enabled:
            raise ValueError(
                "infrastructure metering PV inventory requires its collector"
            )
        if self.pv_inventory_enabled and not self.volume_identity_key_version:
            raise ValueError(
                "infrastructure metering PV inventory requires a volume identity "
                "key version"
            )
        if self.pvc_shadow_enabled:
            missing: list[str] = []
            if not self.pvc_inventory_enabled:
                missing.append("PVC inventory")
            if not self.shadow_enabled:
                missing.append("global shadow mode")
            if missing:
                raise ValueError(
                    "infrastructure metering PVC shadow mode requires "
                    + ", ".join(missing)
                )
        if self.pv_shadow_enabled:
            missing = []
            if not self.pv_inventory_enabled:
                missing.append("PV inventory")
            if not self.shadow_enabled:
                missing.append("global shadow mode")
            if missing:
                raise ValueError(
                    "infrastructure metering PV shadow mode requires "
                    + ", ".join(missing)
                )
        if self.pvc_publication_enabled:
            missing = []
            if not self.publication_enabled:
                missing.append("global publication")
            if not self.pvc_shadow_enabled:
                missing.append("PVC shadow mode")
            if missing:
                raise ValueError(
                    "infrastructure metering PVC publication requires "
                    + ", ".join(missing)
                )
        if self.pv_publication_enabled:
            missing = []
            if not self.publication_enabled:
                missing.append("global publication")
            if not self.pv_shadow_enabled:
                missing.append("PV shadow mode")
            if missing:
                raise ValueError(
                    "infrastructure metering PV publication requires "
                    + ", ".join(missing)
                )
        for label, enabled in (
            ("IDE Pod", self.ide_pod_shadow_enabled),
            ("agent Pod", self.agent_pod_shadow_enabled),
        ):
            if enabled:
                missing = []
                if not self.collector_enabled:
                    missing.append("collector")
                if not self.shadow_enabled:
                    missing.append("global shadow mode")
                if missing:
                    raise ValueError(
                        f"infrastructure metering {label} shadow mode requires "
                        + ", ".join(missing)
                    )
        for label, enabled, shadow_enabled in (
            (
                "IDE Pod",
                self.ide_pod_publication_enabled,
                self.ide_pod_shadow_enabled,
            ),
            (
                "agent Pod",
                self.agent_pod_publication_enabled,
                self.agent_pod_shadow_enabled,
            ),
        ):
            if enabled:
                missing = []
                if not self.publication_enabled:
                    missing.append("global publication")
                if not shadow_enabled:
                    missing.append(f"{label} shadow mode")
                if missing:
                    raise ValueError(
                        f"infrastructure metering {label} publication requires "
                        + ", ".join(missing)
                    )
        if self.vm_inventory_enabled:
            missing = []
            if not self.collector_enabled:
                missing.append("collector")
            if not self.vm_stable_cluster_id:
                missing.append("VM stable cluster id")
            if not self.vm_namespace:
                missing.append("VM namespace")
            if missing:
                raise ValueError(
                    "infrastructure metering VM inventory requires "
                    + ", ".join(missing)
                )
        if self.vm_shadow_enabled:
            missing = []
            if not self.vm_inventory_enabled:
                missing.append("VM inventory")
            if not self.shadow_enabled:
                missing.append("global shadow mode")
            if missing:
                raise ValueError(
                    "infrastructure metering VM shadow mode requires "
                    + ", ".join(missing)
                )
        if self.vm_publication_enabled:
            missing = []
            if not self.publication_enabled:
                missing.append("global publication")
            if not self.vm_shadow_enabled:
                missing.append("VM shadow mode")
            if missing:
                raise ValueError(
                    "infrastructure metering VM publication requires "
                    + ", ".join(missing)
                )
        if self.vm_pvc_inventory_enabled or self.vm_pv_inventory_enabled:
            missing = []
            if not self.collector_enabled:
                missing.append("collector")
            if not self.vm_stable_cluster_id:
                missing.append("VM stable cluster id")
            if not self.vm_namespace:
                missing.append("VM namespace")
            if missing:
                raise ValueError(
                    "infrastructure metering VM storage inventory requires "
                    + ", ".join(missing)
                )
        if self.vm_pv_inventory_enabled:
            missing = []
            if not self.vm_pv_cluster_wide_rbac_acknowledged:
                missing.append("explicit cluster-wide PV RBAC acknowledgement")
            if not self.volume_identity_key_version:
                missing.append("volume identity key version")
            if missing:
                raise ValueError(
                    "infrastructure metering VM PV inventory requires "
                    + ", ".join(missing)
                )
        if self.vm_pvc_shadow_enabled:
            missing = []
            if not self.vm_pvc_inventory_enabled:
                missing.append("VM PVC inventory")
            if not self.shadow_enabled:
                missing.append("global shadow mode")
            if missing:
                raise ValueError(
                    "infrastructure metering VM PVC shadow mode requires "
                    + ", ".join(missing)
                )
        if self.vm_pv_shadow_enabled:
            missing = []
            if not self.vm_pv_inventory_enabled:
                missing.append("VM PV inventory")
            if not self.shadow_enabled:
                missing.append("global shadow mode")
            if missing:
                raise ValueError(
                    "infrastructure metering VM PV shadow mode requires "
                    + ", ".join(missing)
                )
        for label, enabled, shadow_enabled in (
            (
                "VM PVC",
                self.vm_pvc_publication_enabled,
                self.vm_pvc_shadow_enabled,
            ),
            (
                "VM PV",
                self.vm_pv_publication_enabled,
                self.vm_pv_shadow_enabled,
            ),
        ):
            if enabled:
                missing = []
                if not self.publication_enabled:
                    missing.append("global publication")
                if not shadow_enabled:
                    missing.append(f"{label} shadow mode")
                if missing:
                    raise ValueError(
                        f"infrastructure metering {label} publication requires "
                        + ", ".join(missing)
                    )
        if self.collector_enabled and not self.stable_cluster_id:
            raise ValueError(
                "infrastructure metering collector requires a stable cluster id"
            )
        if self.collector_enabled and not self.namespace_allowlist:
            raise ValueError(
                "infrastructure metering collector requires at least one namespace"
            )
        if self.collector_enabled and self.deployment_mode != "dedicated":
            raise ValueError(
                "infrastructure metering in-process collection is not implemented; "
                "use dedicated mode"
            )
        if self.stale_after_seconds < self.relist_interval_seconds:
            raise ValueError(
                "infrastructure metering stale-after must be greater than or "
                "equal to the relist interval"
            )
        if self.diagnostic_retention_days < self.snapshot_item_retention_days:
            raise ValueError(
                "infrastructure metering diagnostic retention must be greater "
                "than or equal to snapshot-item retention"
            )
        if self.source_aware_reads_enabled and not self.v2_reads_enabled:
            raise ValueError(
                "infrastructure metering source-aware reads require v2 reads"
            )
        if self.source_aware_reads_enabled:
            missing: list[str] = []
            if not self.collector_enabled:
                missing.append("collector")
            if not self.shadow_enabled:
                missing.append("shadow")
            if not self.stable_cluster_id:
                missing.append("stable cluster id")
            if missing:
                raise ValueError(
                    "infrastructure metering source-aware reads require "
                    + ", ".join(missing)
                )
        if self.cutover_enabled:
            missing = []
            if not self.collector_enabled:
                missing.append("collector")
            if not self.shadow_enabled:
                missing.append("shadow")
            if not self.stable_cluster_id:
                missing.append("stable cluster id")
            if missing:
                raise ValueError(
                    "infrastructure metering cutover requires " + ", ".join(missing)
                )
        if self.publication_enabled:
            missing: list[str] = []
            if not self.collector_enabled:
                missing.append("collector")
            if not self.shadow_enabled:
                missing.append("shadow")
            if not self.stable_cluster_id:
                missing.append("stable cluster id")
            if missing:
                raise ValueError(
                    "infrastructure metering publication requires " + ", ".join(missing)
                )
