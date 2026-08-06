"""Fail-closed rollout settings for infrastructure metering."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal, Mapping

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})
_STABLE_CLUSTER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
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
    stable_cluster_id: str = ""
    deployment_mode: Literal["dedicated", "in-process"] = "dedicated"
    namespace_allowlist: tuple[str, ...] = ()
    relist_interval_seconds: int = 300
    stale_after_seconds: int = 900
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
            stable_cluster_id=env.get(
                "INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID", ""
            ).strip(),
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
        if self.shadow_enabled and not self.collector_enabled:
            raise ValueError(
                "infrastructure metering shadow mode requires its collector"
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
